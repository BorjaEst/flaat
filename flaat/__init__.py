"""FLAsk support for OIDC Access Tokens -- FLAAT. A set of decorators for authorising
access to OIDC authenticated REST APIs."""
# This code is distributed under the MIT License

# pylint: disable=logging-fstring-interpolation,fixme
# TODO remove these disables

import json
import logging
import os
from functools import wraps
from queue import Empty, Queue
from threading import Thread
from typing import Any, Callable, Dict, List, Literal, Tuple, Union

from aarc_g002_entitlement import Aarc_g002_entitlement, Aarc_g002_entitlement_Error

from flaat import issuertools, tokentools
from flaat.caches import Issuer_config_cache
from flaat.exceptions import FlaatException, FlaatForbidden, FlaatUnauthorized

logger = logging.getLogger(__name__)

# defaults; May be overwritten per initialisation of flaat
VERIFY_TLS = True

# No leading slash ('/') in ops_that_support_jwt !!!
OPS_THAT_SUPPORT_JWT = [
    "https://iam-test.indigo-datacloud.eu",
    "https://iam.deep-hybrid-datacloud.eu",
    "https://iam.extreme-datacloud.eu",
    "https://wlcg.cloud.cnaf.infn.it",
    "https://aai.egi.eu/oidc",
    "https://aai-dev.egi.eu/oidc",
    "https://oidc.scc.kit.edu/auth/realms/kit",
    "https://unity.helmholtz-data-federation.de/oauth2",
    "https://login.helmholtz-data-federation.de/oauth2",
    "https://login-dev.helmholtz.de/oauth2",
    "https://login.helmholtz.de/oauth2",
    "https://b2access.eudat.eu/oauth2",
    "https://b2access-integration.fz-juelich.de/oauth2",
    "https://services.humanbrainproject.eu/oidc",
    "https://login.elixir-czech.org/oidc",
]


def ensure_is_list(item: Union[list, str]) -> List[str]:
    """Make sure we have a list"""
    if isinstance(item, str):
        return [item]
    return item


def check_environment_for_override(env_key):
    """Override the actual group membership, if environment is set."""
    env_val = os.getenv(env_key)
    try:
        if env_val is not None:
            avail_entitlement_entries = json.loads(env_val)
            return avail_entitlement_entries
    except TypeError as e:
        logger.error(
            f"Cannot decode JSON group list from the environment:" f"{env_val}\n{e}"
        )
    except json.JSONDecodeError as e:
        logger.error(
            f"Cannot decode JSON group list from the environment:" f"{env_val}\n{e}"
        )
    return None


def formatted_entitlements(entitlements):
    def my_mstr(self):
        """Return the nicely formatted entitlement"""
        str_str = "\n".join(
            [
                "    namespace_id:        {namespace_id}"
                + "\n    delegated_namespace: {delegated_namespace}"
                + "\n    subnamespaces:       {subnamespaces}"
                + "\n    group:               {group}"
                + "\n    subgroups:           {subgroups}"
                + "\n    role_in_subgroup     {role}"
                + "\n    group_authority:     {group_authority}"
            ]
        ).format(
            namespace_id=self.namespace_id,
            delegated_namespace=self.delegated_namespace,
            group=self.group,
            group_authority=self.group_authority,
            subnamespaces=",".join([f"{ns}" for ns in self.subnamespaces]),
            subgroups=",".join([f"{grp}" for grp in self.subgroups]),
            role=f"{self.role}" if self.role else "n/a",
        )
        return str_str

    return "\n" + "\n\n".join([my_mstr(x) for x in entitlements]) + "\n"


class FlaatConfig:
    def __init__(self):
        self.trusted_op_list: List[str] = []
        self.iss: str = ""
        self.op_hint: str = ""
        self.trusted_op_file: str = ""
        self.verify_tls: bool = True
        self.client_id: str = ""
        self.client_secret: str = ""
        self.num_request_workers: int = 10
        self.client_connect_timeout: float = 1.2  # seconds
        self.ops_that_support_jwt: List[str] = OPS_THAT_SUPPORT_JWT
        self.claim_search_precedence: List[str] = ["userinfo", "access_token"]
        self.raise_error_on_return = True  # else just return an error

    def set_cache_lifetime(self, lifetime):
        """Set cache lifetime of requests_cache zn seconds, default: 300s"""
        issuertools.cache_options.set_lifetime(lifetime)

    def set_cache_allowable_codes(self, allowable_codes):
        """set http status code that will be cached"""
        issuertools.cache_options.set_allowable_codes(allowable_codes)

    def set_cache_backend(self, backend):
        """set the cache backend"""
        issuertools.cache_options.backend = backend

    def set_trusted_OP(self, iss):
        """Define OIDC Provider. Must be a valid URL. E.g. 'https://aai.egi.eu/oidc/'
        This should not be required for OPs that put their address into the AT (e.g. keycloak, mitre,
        shibboleth)"""
        self.iss = iss.rstrip("/")

    def set_trusted_OP_list(self, trusted_op_list: List[str]):
        """Define a list of OIDC provider URLs.
        E.g. ['https://iam.deep-hybrid-datacloud.eu/', 'https://login.helmholtz.de/oauth2/', 'https://aai.egi.eu/oidc/']"""
        self.trusted_op_list = []
        for issuer in trusted_op_list:
            self.trusted_op_list.append(issuer.rstrip("/"))

        # iss_config = issuertools.find_issuer_config_in_list(self.trusted_op_list, self.op_hint,
        #         exclude_list = [])
        # self.issuer_config_cache.add_list(iss_config)

    def set_trusted_OP_file(self, filename="/etc/oidc-agent/issuer.config", hint=None):
        """Set filename of oidc-agent's issuer.config. Requires oidc-agent to be installed."""
        self.trusted_op_file = filename
        self.op_hint = hint

    def set_OP_hint(self, hint):
        """String to specify the hint. This is used for regex searching in lists of providers for
        possible matching ones."""
        self.op_hint = hint

    def set_verify_tls(self, param_verify_tls=True):
        """Whether to verify tls connections. Only use for development and debugging"""
        self.verify_tls = param_verify_tls
        issuertools.verify_tls = param_verify_tls

    def set_client_id(self, client_id):
        """Client id. At the moment this one is sent to all matching providers. This is only
        required if you need to access the token introspection endpoint. I don't have a use case for
        that right now."""
        # FIXME: consider client_id/client_secret per OP.
        self.client_id = client_id

    def set_client_secret(self, client_secret):
        """Client Secret. At the moment this one is sent to all matching providers."""
        self.client_secret = client_secret

    def set_num_request_workers(self, num):
        """set number of request workers"""
        self.num_request_workers = num
        issuertools.num_request_workers = num

    def get_num_request_workers(self):
        """get number of request workers"""
        return self.num_request_workers

    def set_client_connect_timeout(self, num):
        """set timeout for flaat connecting to OPs"""
        self.client_connect_timeout = num

    def get_client_connect_timeout(self):
        """get timeout for flaat connecting to OPs"""
        return self.client_connect_timeout

    def set_iss_config_timeout(self, num):
        """set timeout for connections to get config from OP"""
        issuertools.timeout = num

    def get_iss_config_timeout(self):
        """set timeout for connections to get config from OP"""
        return issuertools.timeout

    def set_timeout(self, num):
        """set global timeouts for http connections"""
        self.set_iss_config_timeout(num)
        self.set_client_connect_timeout(num)

    def get_timeout(self):
        """get global timeout for https connections"""
        return (self.get_iss_config_timeout(), self.get_client_connect_timeout())

    def set_claim_search_precedence(self, a_list):
        """set order in which to search for specific claim"""
        self.claim_search_precedence = a_list

    def get_claim_search_precedence(self):
        """get order in which to search for specific claim"""
        return self.claim_search_precedence


class BaseFlaat(FlaatConfig):
    """FLAsk support for OIDC Access Tokens.
    Provide decorators and configuration for OIDC"""

    def __init__(self):
        super().__init__()
        self.issuer_config_cache = Issuer_config_cache()
        # maps issuer to issuer configs
        self.accesstoken_issuer_cache: Dict[str, str] = {}  # maps accesstoken to issuer
        # self.request_id = "unset"

    # SUBCLASS STUBS
    def get_request_id(self, request_object) -> str:
        _ = request_object
        # raise NotImplementedError("use framework specific sub class")
        return ""

    def _get_request(self, *args, **kwargs):
        """overwritten in subclasses"""
        # raise NotImplementedError("implement in subclass")
        _ = args
        _ = kwargs
        return {}

    def _map_exception(self, exception: FlaatException):
        _ = exception

    def _wrap_async_call(self, func, *args, **kwargs):
        """may be overwritten in in sub class"""
        return func(*args, **kwargs)

    # FIXME broken! this expects the request from flask, but we support other frameworks as well
    def get_access_token_from_request(self, request) -> str:
        """Helper function to obtain the OIDC AT from the flask request variable"""
        return ""
        # if request.headers.get("Authorization", "").startswith("Bearer "):
        #     temp = request.headers["Authorization"].split("authorization header: ")[0]
        #     token = temp.split(" ")[1]
        #     return token
        # elif "access_token" in request.form:
        #     return request.form["access_token"]
        # elif "access_token" in request.args:
        #     return request.args["access_token"]

        # raise FlaatUnauthorized("No access token")

    # END SUBCLASS STUBS

    # TODO this method is way too long
    def _find_issuer_config_everywhere(self, access_token):
        """Use many places to find issuer configs"""

        # 0: Use accesstoken_issuer cache to find issuerconfig:
        logger.debug("0: Trying to find issuer in cache")
        if access_token in self.accesstoken_issuer_cache:
            issuer = self.accesstoken_issuer_cache[access_token]
            iss_config = self.issuer_config_cache.get(issuer)
            logger.debug(f"  0: returning {iss_config}")
            return [iss_config]

        # 1: find info in the AT
        logger.debug("1: Trying to find issuer in access_token")
        at_iss = tokentools.get_issuer_from_accesstoken_info(access_token)
        if at_iss is not None:
            trusted_op_list_buf = []
            if self.trusted_op_list is not None:
                if len(self.trusted_op_list) > 0:
                    trusted_op_list_buf = self.trusted_op_list
            if self.iss is not None:
                trusted_op_list_buf.append(self.iss)
            if at_iss.rstrip("/") not in trusted_op_list_buf:
                raise FlaatForbidden(
                    f"The issuer {at_iss} of the received access_token is not trusted"
                )

        iss_config = issuertools.find_issuer_config_in_at(access_token)
        if iss_config is not None:
            return [iss_config]

        # 2: use a provided string
        logger.debug('2: Trying to find issuer from "set_iss"')
        iss_config = issuertools.find_issuer_config_in_string(self.iss)
        if iss_config is not None:
            return [iss_config]

        # 3: Try the provided list of providers:
        logger.debug("3: Trying to find issuer from trusted_op_list")
        iss_config = issuertools.find_issuer_config_in_list(
            self.trusted_op_list, self.op_hint, exclude_list=self.ops_that_support_jwt
        )
        if iss_config is not None:
            return iss_config

        # 4: Try oidc-agent's issuer config file
        logger.debug('Trying to find issuer from "set_OIDC_provider_file"')
        iss_config = issuertools.find_issuer_config_in_file(
            self.trusted_op_file, self.op_hint, exclude_list=self.ops_that_support_jwt
        )
        if iss_config is not None:
            return iss_config

        raise FlaatForbidden("Issuer config not found")

    def get_info_thats_in_at(self, access_token):
        # FIXME: Add here parameter verify=True, then go and verify the token
        """return the information contained inside the access_token itself"""
        accesstoken_info = None
        if access_token:
            accesstoken_info = tokentools.get_accesstoken_info(access_token)
        return accesstoken_info

    def get_issuer_from_accesstoken(self, access_token: str):
        """get the issuer that issued the accesstoken"""
        if access_token in self.accesstoken_issuer_cache:
            issuer = self.accesstoken_issuer_cache[access_token]
            return issuer

        # this also updates the cache
        (_, issuer_config) = self._get_info_from_userinfo_endpoints(access_token)
        return issuer_config["issuer"]

    def _get_info_from_userinfo_endpoints(self, access_token: str) -> Tuple[dict, dict]:
        """Traverse all reasonable configured userinfo endpoints and query them with the
        access_token. Note: For OPs that include the iss inside the AT, they will be directly
        queried, and are not included in the search (because that makes no sense).

        Also updates
            - accesstoken_issuer_cache
            - issuer_config_cache
        """
        user_info = None  # return value

        # get a sensible issuer config. In case we don't have a jwt AT, we poll more OPs
        issuer_config_list = self._find_issuer_config_everywhere(access_token)
        self.issuer_config_cache.add_list(issuer_config_list)

        # If there is no issuer in the cache by now, we're dead
        if len(self.issuer_config_cache) == 0:
            raise FlaatUnauthorized("No issuer config found, or issuer not supported")

        # get userinfo
        param_q = Queue(self.num_request_workers * 2)
        result_q = Queue(self.num_request_workers * 2)

        def thread_worker_get_userinfo():
            """Thread worker"""

            def safe_get(q):
                try:
                    return q.get(timeout=5)
                except Empty:
                    return None

            while True:
                item = safe_get(param_q)
                if item is None:
                    break
                result = issuertools.get_user_info(
                    item["access_token"], item["issuer_config"]
                )
                result_q.put(result)
                param_q.task_done()
                result_q.task_done()

        for _ in range(self.num_request_workers):
            t = Thread(target=thread_worker_get_userinfo)
            t.daemon = True
            t.start()

        for issuer_config in self.issuer_config_cache:
            # logger.info(F"tyring to get userinfo from {issuer_config['issuer']}")
            # user_info = issuertools.get_user_info(access_token, issuer_config)
            params = {}
            params["access_token"] = access_token
            params["issuer_config"] = issuer_config
            param_q.put(params)

        # Collect results from threadpool
        param_q.join()
        result_q.join()
        try:
            while not result_q.empty():
                retval = result_q.get(block=False, timeout=self.client_connect_timeout)
                if retval is not None:
                    (user_info, issuer_config) = retval
                    issuer = issuer_config["issuer"]
                    self.issuer_config_cache.add_config(issuer, issuer_config)
                    # logger.info(F"storing in accesstoken cache: {issuer} -=> {access_token}")
                    self.accesstoken_issuer_cache[access_token] = issuer
                    return (user_info, issuer_config)
        except Empty:
            logger.info("EMPTY result in thead join")

        raise FlaatUnauthorized(
            "User Info not found or not accessible. Something may be wrong with the Access Token."
        )

    def get_info_from_userinfo_endpoints(self, access_token: str) -> dict:
        (userinfo, _) = self._get_info_from_userinfo_endpoints(access_token)
        return userinfo

    def get_info_from_introspection_endpoints(
        self, access_token: str
    ) -> Union[dict, None]:
        """If there's a client_id and client_secret defined, we access the token introspection
        endpoint and return the info obtained from there"""
        # get introspection_token
        introspection_info = None

        # TODO this looks totaly broken
        issuer_config_list = self._find_issuer_config_everywhere(access_token)
        self.issuer_config_cache.add_list(issuer_config_list)

        if len(self.issuer_config_cache) == 0:
            logger.info("Issuer Configs yielded None")
            # self.set_last_error("Issuer of Access Token is not supported")
            return None
        for issuer_config in self.issuer_config_cache:
            introspection_info = issuertools.get_introspected_token_info(
                access_token, issuer_config, self.client_id, self.client_secret
            )
            if introspection_info is not None:
                break
        return introspection_info

    def get_all_info_by_at(self, access_token: str):
        """Collect all possible user info and return them as one json
        object."""

        accesstoken_info = self.get_info_thats_in_at(access_token)
        user_info = self.get_info_from_userinfo_endpoints(access_token)
        introspection_info = self.get_info_from_introspection_endpoints(access_token)
        # FIXME: We have to verify the accesstoken
        # And verify that it comes from a trusted issuer!!

        if accesstoken_info is not None:
            timeleft = tokentools.get_timeleft(accesstoken_info)

            if timeleft is not None and timeleft < 0:
                raise FlaatUnauthorized("Token expired for {abs(timeleft)} seconds")

        if user_info is None:
            return None

        return tokentools.merge_tokens(
            [accesstoken_info, user_info, introspection_info]
        )

    def _get_all_info_from_request(self, param_request):
        """gather all info about the user that we can find.
        Returns a "supertoken" json structure."""
        access_token = self.get_access_token_from_request(param_request)

        return self.get_all_info_by_at(access_token)

    def _auth_disabled(self):
        return (
            "yes"
            == os.environ.get(
                "DISABLE_AUTHENTICATION_AND_ASSUME_AUTHENTICATED_USER", ""
            ).lower()
        )

    def _auth_get_all_info(self, *args, **kwargs):
        request_object = self._get_request(*args, **kwargs)
        return self._get_all_info_from_request(request_object)

    def _determine_number_of_required_matches(self, match, req_group_list) -> int:
        """determine the number of required matches from parameters"""
        # How many matches do we need?
        required_matches = None
        if match == "all":
            required_matches = len(req_group_list)
        elif match == "one":
            required_matches = 1
        elif isinstance(match, int):
            required_matches = match
            if required_matches > len(req_group_list):
                required_matches = len(req_group_list)
        else:
            raise FlaatException(
                "Argument 'match' has invalid value: Must be 'all', 'one' or int"
            )

        return required_matches

    def _get_entitlements_from_claim(self, all_info: dict, claim: str) -> List[str]:
        """extract groups / entitlements from given claim (in userinfo or access_token)"""
        # search group / entitlement entries in specified claim (in userinfo or access_token)
        avail_group_entries = None
        for location in self.claim_search_precedence:
            avail_group_entries = None
            if location == "userinfo":
                avail_group_entries = all_info.get(claim)
            if location == "access_token":
                avail_group_entries = all_info["body"].get(claim)
            if avail_group_entries is not None:
                break

        if avail_group_entries is None:
            raise FlaatUnauthorized(f"Not authorised (claim does not exist: {claim})")
        if not isinstance(avail_group_entries, list):
            raise FlaatUnauthorized(
                f"Not authorised (claim does not point to a list: {avail_group_entries})"
            )

        return avail_group_entries

    def _get_effective_entitlements_from_claim(
        self, all_info: dict, claim: str
    ) -> List[str]:
        override_entitlement_entries = check_environment_for_override(
            "DISABLE_AUTHENTICATION_AND_ASSUME_ENTITLEMENTS"
        )
        if override_entitlement_entries is not None:
            logger.info("Using entitlement override: %s", override_entitlement_entries)
            return override_entitlement_entries

        return self._get_entitlements_from_claim(all_info, claim)

    def _required_auth_func(
        self,
        required: Union[str, List[str]],
        claim: str,
        *args,
        match: Union[Literal["all"], Literal["one"], int] = "all",
        # parse an entitlement
        parser: Callable[[str], Any] = None,
        # compare two parsed entitlements
        comparator: Callable[[Any, Any], bool] = None,
        **kwargs,
    ):
        request_object = self._get_request(*args, **kwargs)
        all_info = self._get_all_info_from_request(request_object)

        if all_info is None:
            raise FlaatUnauthorized("No valid authentication found.")

        req_raw = ensure_is_list(required)
        avail_raw = self._get_effective_entitlements_from_claim(all_info, claim)
        if avail_raw is None:
            raise FlaatUnauthorized("No group memberships found")

        req_parsed = []
        avail_parsed = []

        if parser is None:
            req_parsed = req_raw
            avail_parsed = avail_raw
        else:
            req_parsed = [parser(r) for r in req_raw]
            avail_parsed = [parser(r) for r in avail_raw]

        required_matches = self._determine_number_of_required_matches(match, req_parsed)
        matches_found = 0
        if comparator is None:
            comparator = lambda r, a: r == a

        for req in req_parsed:
            for avail in avail_parsed:
                if comparator(req, avail):
                    matches_found += 1

        logger.info("Found %d of %d matches", matches_found, required_matches)
        logger.debug("Required: %s", req_parsed)
        logger.debug("Available: %s", avail_parsed)

        if matches_found < required_matches:
            raise FlaatForbidden(
                f"Matched {matches_found} groups, but needed {required_matches}"
            )

    # TODO test this
    def _get_auth_decorator(
        self,
        auth_func: Callable,
        on_failure: Callable[[FlaatException], Any] = None,
    ):
        def decorator(view_func):
            @wraps(view_func)
            async def wrapper(*args, **kwargs):
                # notable: auth_func and view_func get the same arguments
                try:
                    if not self._auth_disabled():
                        # auth_func raises an exception if unauthorized
                        auth_func(self, *args, **kwargs)

                    return await view_func(*args, **kwargs)
                except FlaatException as e:
                    if on_failure is not None:
                        return on_failure(e)

                    self._map_exception(e)

            return wrapper

        return decorator

    def login_required(self, on_failure: Callable = None):
        if on_failure is not None and not callable(on_failure):
            raise ValueError("Invalid argument: need callable")

        return self._get_auth_decorator(auth_func=self._auth_get_all_info)

    def group_required(
        self,
        group: Union[str, List[str]],
        claim: str,
        on_failure: Callable = None,
        match: Union[Literal["all"], Literal["one"], int] = "all",
    ):
        """Decorator to enforce membership in a given group.
        group is the name (or list) of the group to match
        match specifies how many of the given groups must be matched. Valid values for match are
        'all', 'one', or an integer
        on_failure is a function that will be invoked if there was no valid user detected.
        Useful for redirecting to some login page"""

        def auth_func(self, *args, **kwargs):
            self._required_auth_func(group, claim, match, *args, **kwargs)

        return self._get_auth_decorator(auth_func, on_failure)

    @staticmethod
    def _aarc_entitlement_parser(
        entitlement: str,
    ):
        try:
            return Aarc_g002_entitlement(entitlement)
        except Aarc_g002_entitlement_Error as e:
            logger.error("Error parsing aarc entitlement: %s", e)
            return None

    @staticmethod
    def _aarc_entitlement_comparator(
        req: Aarc_g002_entitlement, avail: Aarc_g002_entitlement
    ) -> bool:
        return req.is_contained_in(avail)

    def aarc_g002_entitlement_required(
        self,
        entitlement: Union[str, List[str]],
        claim: str,
        on_failure: Callable = None,
        match: Union[Literal["all"], Literal["one"], int] = "all",
    ):
        """Decorator to enforce membership in a given group defined according to AARC-G002.

        group is the name (or list) of the entitlement to match
        match specifies how many of the given groups must be matched. Valid values for match are
        'all', 'one', or an integer
        on_failure is a function that will be invoked if there was no valid user detected.
        Useful for redirecting to some login page
        """

        def auth_func(self, *args, **kwargs):
            self._required_auth_func(
                entitlement,
                claim,
                match,
                parser=self._aarc_entitlement_parser,
                comparator=self._aarc_entitlement_comparator,
                *args,
                **kwargs,
            )

        return self._get_auth_decorator(auth_func, on_failure)
