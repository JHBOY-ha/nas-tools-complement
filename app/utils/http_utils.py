import requests
from config import Config


class RequestUtils:
    _headers = None
    _cookies = None
    _proxies = None
    _timeout = 20
    _session = None

    def __init__(self,
                 headers=None,
                 cookies=None,
                 proxies=False,
                 session=None,
                 timeout=None,
                 referer=None,
                 content_type=None,
                 verify=True,
                 isolated=False,
                 deadline=None,
                 max_bytes=20 * 1024 * 1024):
        if not content_type:
            content_type = "application/x-www-form-urlencoded; charset=UTF-8"
        if headers:
            if isinstance(headers, str):
                self._headers = {
                    "Content-Type": content_type,
                    "User-Agent": f"{headers}"
                }
            else:
                self._headers = headers
        else:
            self._headers = {
                "Content-Type": content_type,
                "User-Agent": Config().get_ua()
            }
        if referer:
            self._headers.update({
                "referer": referer
            })
        if cookies:
            if isinstance(cookies, str):
                self._cookies = self.cookie_parse(cookies)
            else:
                self._cookies = cookies
        # Preserve full Requests maps, including all, no_proxy and host selectors.
        if isinstance(proxies, dict) and any(proxies.values()):
            self._proxies = proxies
        if session:
            self._session = session
        if timeout:
            self._timeout = timeout
        # Verify certificates by default; requests also accepts a private CA path.
        self._verify = verify
        # Opt-in transport keeps the existing RequestUtils interface available
        # to providers while moving DNS/stream reads into bounded subprocesses.
        self._isolated = isolated
        self._deadline = deadline
        self._max_bytes = max_bytes

    def post(self, url, params=None, json=None):
        if json is None:
            json = {}
        try:
            if self._session:
                return self._session.post(url,
                                          data=params,
                                          verify=self._verify,
                                          headers=self._headers,
                                          proxies=self._proxies,
                                          timeout=self._timeout,
                                          json=json)
            else:
                return requests.post(url,
                                     data=params,
                                     verify=self._verify,
                                     headers=self._headers,
                                     proxies=self._proxies,
                                     timeout=self._timeout,
                                     json=json)
        except requests.exceptions.RequestException:
            return None

    def get(self, url, params=None):
        try:
            if self._session:
                r = self._session.get(url,
                                      verify=self._verify,
                                      headers=self._headers,
                                      proxies=self._proxies,
                                      timeout=self._timeout,
                                      params=params)
            else:
                r = requests.get(url,
                                 verify=self._verify,
                                 headers=self._headers,
                                 proxies=self._proxies,
                                 timeout=self._timeout,
                                 params=params)
            # Prefer declared encodings, retain strict UTF-8 when absent, and
            # return the existing failure sentinel rather than leaking decode errors.
            declared = 'charset' in r.headers.get('Content-Type', '').lower()
            return r.content.decode((r.encoding if declared else None) or 'utf-8')
        except (UnicodeError, LookupError):
            return None
        except requests.exceptions.RequestException:
            return None

    def get_res(self, url, params=None, allow_redirects=True, stream=False):
        try:
            if self._isolated:
                from app.utils.isolated_network import bounded_request
                return bounded_request(url, params=params, headers=self._headers,
                                       proxies=self._proxies, cookies=self._cookies,
                                       timeout=self._timeout, verify=self._verify,
                                       allow_redirects=allow_redirects, stream=stream,
                                       deadline=self._deadline, max_bytes=self._max_bytes)
            if self._session:
                return self._session.get(url,
                                         params=params,
                                         verify=self._verify,
                                         headers=self._headers,
                                         proxies=self._proxies,
                                         cookies=self._cookies,
                                         timeout=self._timeout,
                                         allow_redirects=allow_redirects,
                                         stream=stream)
            else:
                return requests.get(url,
                                    params=params,
                                    verify=self._verify,
                                    headers=self._headers,
                                    proxies=self._proxies,
                                    cookies=self._cookies,
                                    timeout=self._timeout,
                                    allow_redirects=allow_redirects,
                                    stream=stream)
        except requests.exceptions.RequestException:
            return None

    def post_res(self, url, params=None, allow_redirects=True, files=None, json=None,
                 stream=False):
        try:
            if self._isolated:
                if files is not None:
                    raise ValueError('Isolated HTTP transport does not accept upload handles')
                from app.utils.isolated_network import bounded_request
                return bounded_request(url, method='POST', json=json, headers=self._headers,
                                       proxies=self._proxies, cookies=self._cookies,
                                       timeout=self._timeout, verify=self._verify,
                                       allow_redirects=allow_redirects, stream=stream,
                                       deadline=self._deadline, max_bytes=self._max_bytes)
            if self._session:
                return self._session.post(url,
                                          data=params,
                                          verify=self._verify,
                                          headers=self._headers,
                                          proxies=self._proxies,
                                          cookies=self._cookies,
                                          timeout=self._timeout,
                                          allow_redirects=allow_redirects,
                                          files=files,
                                          json=json,
                                          stream=stream)
            else:
                return requests.post(url,
                                     data=params,
                                     verify=self._verify,
                                     headers=self._headers,
                                     proxies=self._proxies,
                                     cookies=self._cookies,
                                     timeout=self._timeout,
                                     allow_redirects=allow_redirects,
                                     files=files,
                                     json=json,
                                     stream=stream)
        except requests.exceptions.RequestException:
            return None

    @staticmethod
    def cookie_parse(cookies_str, array=False):
        if not cookies_str:
            return {}
        cookie_dict = {}
        cookies = cookies_str.split(';')
        for cookie in cookies:
            cstr = cookie.split('=')
            if len(cstr) > 1:
                cookie_dict[cstr[0].strip()] = cstr[1].strip()
        if array:
            cookiesList = []
            for cookieName, cookieValue in cookie_dict.items():
                cookies = {'name': cookieName, 'value': cookieValue}
                cookiesList.append(cookies)
            return cookiesList
        return cookie_dict
