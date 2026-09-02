import contextlib
import re
from dataclasses import dataclass
from multiprocessing.managers import DictProxy
from typing import Any

import httpx
from sanic import Sanic
from sanic.log import Colors, logger

from app.core.constants import ENCODING
from app.core.dl.rpc.models import API, Method, RpcConfig
from app.core.exceptions import ErrorCode, KaloscopeException
from app.core.renderer import jsonpath_all, jsonpath_first, render
from app.utils.json import JSONType, dumps, try_loads


@dataclass(slots=True)
class RpcClient:
    config: RpcConfig

    @property
    def csrf_tokens(self) -> DictProxy[str, str]:
        return Sanic.get_app().shared_ctx.csrf_tokens

    async def version(self) -> str | None:
        """Get the version of the RPC endpoint.

        Returns:
            The version of the RPC endpoint.
        """
        version = None
        with contextlib.suppress(Exception):
            result = await self.call("version")
            if isinstance(result, dict):
                version = str(result.get("version", ""))
            elif isinstance(result, str):
                version = result
        # remove the build number from the version
        if version:
            version = re.sub(r"\s*\(.*\)", "", version)
        return version

    async def call(self, method: Method, variables: dict | None = None) -> JSONType:
        """Call an API method with the given variables.

        Args:
            method: The method name.
            variables: The variables to pass to the API.

        Returns:
            The result of the API call.
        """
        api = self.config.methods.get(method)
        if api is not None:
            if variables is None:
                variables = {}
            # add authentication to the variables
            if self.config.auth is not None:
                variables["secret"] = self.config.auth.secret or ""
                variables["username"] = self.config.auth.username or ""
                variables["password"] = self.config.auth.password or ""
            return await self._request(api, variables, api_method=method)

    async def _request(
        self,
        api: API,
        variables: dict,
        *,
        api_method: Method,
        retries: int = 0,
    ) -> JSONType:
        """Make an HTTP request to the given API with the given variables.

        Args:
            api: The API schema.
            variables: The variables to render the API with.
            api_method: The configured RPC method.
            retries: The number of retries.

        Returns:
            The result of the API call.
        """
        if retries >= 2:
            logger.error("The request has been retried %d times.", retries)
            raise KaloscopeException(ErrorCode.HTTP_REQUEST_FAILED)

        method = "GET" if api.get else "POST" if api.post else "POST"
        url = _render(f"{self.config.base_url}{api.get or api.post or ''}", variables)
        # request headers
        headers = _render(api.headers or {}, variables)
        csrf_header = self.config.csrf_header
        if csrf_header and (token := self.csrf_tokens.get(self.config.base_url)):
            headers[csrf_header] = token
        # request body
        content, data, files, json = self._request_body(api, variables)
        # basic auth
        auth = self.config.basic_auth if not self.config.explicit_login else None
        # make the request
        client: httpx.AsyncClient = Sanic.get_app().ctx.httpx
        try:
            return await self._response(
                api,
                variables,
                await client.request(
                    method,
                    url,
                    headers=headers,
                    content=content,
                    data=data,
                    files=files,
                    json=json,
                    auth=auth,
                ),
                api_method=api_method,
                retries=retries,
            )
        except httpx.RequestError:
            logger.error("An error occurred while requesting the downloader.")
            raise KaloscopeException(ErrorCode.HTTP_REQUEST_FAILED) from None

    def _request_body(self, api: API, variables: dict) -> tuple:
        """Get the request body for the given API and variables.

        Args:
            api: The API schema.
            variables: The variables to render the API with.

        Returns:
            The request body tuple.
        """
        content, data, files, json = None, None, None, None
        if api.body is not None:
            # binary request data
            body = _render(api.body, variables, raw=True)
            content = body.encode(ENCODING) if isinstance(body, str) else dumps(body)
        elif api.form is not None:
            # form encoded data
            form = _render(api.form, variables, raw=True)
            for key, value in form.items():
                if _is_multipart_file(value):
                    if files is None:
                        files = {}
                    files[key] = value
                else:
                    if data is None:
                        data = {}
                    data[key] = value
        elif api.json_ is not None:
            # json encoded data
            if isinstance(api.json_, dict | list | str):
                json = _render(api.json_, variables, raw=True)
            else:
                json = api.json_
        return content, data, files, json

    async def _response(
        self,
        api: API,
        variables: dict,
        response: httpx.Response,
        *,
        api_method: Method,
        retries: int,
    ) -> JSONType:
        """Process the HTTP response with the given API schema.

        Args:
            api: The API schema.
            variables: The variables to render the API with.
            response: The HTTP response.
            api_method: The configured RPC method.
            retries: The number of retries.

        Returns:
            The result of the API call.
        """
        # call the login method if the status code is 403
        if response.status_code == 403 and self.config.explicit_login:
            if api_method != "login":
                await self.call("login")
            return await self._request(
                api,
                variables,
                api_method=api_method,
                retries=retries + 1,
            )

        # update the CSRF token if the status code is 409
        csrf_header = self.config.csrf_header
        if response.status_code == 409 and csrf_header:
            self.csrf_tokens[self.config.base_url] = response.headers.get(csrf_header)
            return await self._request(
                api,
                variables,
                api_method=api_method,
                retries=retries + 1,
            )

        # raise an exception if the status code is not 2xx
        if not 200 <= response.status_code < 300:
            logger.error(
                "The request failed with status code %d.", response.status_code
            )
            extra = {"responded": True, "status_code": response.status_code}
            if error_msg := _error_msg(response):
                raise KaloscopeException(error_msg, extra=extra)
            raise KaloscopeException(ErrorCode.HTTP_REQUEST_FAILED, extra=extra)

        # raise an exception if the response is not successful
        if _failed(self._unexpected(api), response.text) or (
            not _successful(self._expected(api), response.text)
        ):
            logger.debug(f"HTTP Response: {Colors.RED}%s{Colors.END}", response.text)
            raise KaloscopeException(ErrorCode.HTTP_REQUEST_FAILED)

        # process the response based on the API schema
        if response.text and (json := try_loads(response.text)):
            logger.debug(f"HTTP Response: {Colors.GREEN}%s{Colors.END}", json)
            if api.response and api.response.mappings:
                if api.response.each:
                    items = jsonpath_all(json, api.response.each)
                    return [_mapping(item, api.response.mappings) for item in items]
                else:
                    return _mapping(json, api.response.mappings)
            return json
        return response.text

    def _expected(self, api: API) -> dict[str, str] | str | None:
        """Get the expected response format for the given API.

        Args:
            api: The API schema.

        Returns:
            The expected response format.
        """
        if api.response and api.response.expected:
            return api.response.expected
        elif self.config.response and self.config.response.expected:
            return self.config.response.expected
        return None

    def _unexpected(self, api: API) -> dict[str, str] | str | None:
        """Get the unexpected response format for the given API.

        Args:
            api: The API schema.

        Returns:
            The unexpected response format.
        """
        if api.response and api.response.unexpected:
            return api.response.unexpected
        elif self.config.response and self.config.response.unexpected:
            return self.config.response.unexpected
        return None


def _successful(expected: dict[str, str] | str | None, actual: JSONType) -> bool:
    """Check if the response is successful based on the expected format.

    Args:
        expected: The expected response format.
        actual: The actual response value.

    Returns:
        Whether the response is successful.
    """
    if expected is None:
        return True
    if isinstance(expected, dict):
        return all(
            code == jsonpath_first(actual, path) for code, path in expected.items()
        )
    return actual == expected


def _failed(unexpected: dict[str, str] | str | None, actual: JSONType) -> bool:
    """Check if the response is failed based on the unexpected format.

    Args:
        unexpected: The unexpected response format.
        actual: The actual response value.

    Returns:
        Whether the response is failed.
    """
    if unexpected is None:
        return False
    if isinstance(unexpected, dict):
        return any(
            code == jsonpath_first(actual, path) for code, path in unexpected.items()
        )
    return actual == unexpected


def _error_msg(response: httpx.Response) -> str | None:
    """Extract the error message from the HTTP response if possible.

    Args:
        response: The HTTP response.

    Returns:
        The error message if found, otherwise None.
    """
    try:
        if response.text and isinstance((json := try_loads(response.text)), dict):
            error = json.get("error")
            if isinstance(error, dict) and "message" in error:
                return str(error["message"])
    except Exception:
        pass
    return None


def _render[T: dict | list | str](value: T, variables: dict, *, raw: bool = False) -> T:
    """Render the given value with the given variables.

    Args:
        value: The value to render.
        variables: The variables to render the value with.
        raw: Whether to render the value as raw object.

    Returns:
        The rendered value.
    """
    if not variables:
        return value
    return render(value, variables, raw=raw)


def _mapping(json: Any, mappings: dict[str, str]) -> dict[str, Any]:
    """Map the JSON response with the given mappings using JSONPath.

    Args:
        json: The JSON response.
        mappings: The mappings dictionary.

    Returns:
        The mapped JSON response.
    """

    def jsonpath(k: str):
        # determine the JSONPath function based on the mapping key
        return jsonpath_all if k == "files" else jsonpath_first

    return {k: jsonpath(k)(json, v) for k, v in mappings.items()}


def _is_multipart_file(value: Any) -> bool:
    """Check if the given value is a multipart file tuple.

    Args:
        value: The value to check.

    Returns:
        Whether the value is a multipart file tuple.
    """
    if not isinstance(value, tuple):
        return False
    if len(value) < 2:
        return False
    if not isinstance(value[0], str | type(None)):
        return False
    return isinstance(value[1], bytes)
