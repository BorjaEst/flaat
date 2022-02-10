"""This module contains classes to express diverse requiremnts which a user needs to satisfy in order to use a view function"""
from dataclasses import dataclass
import json
import logging
import os
from typing import Any, Callable, List, Optional, Union

import aarc_entitlement

from flaat.exceptions import FlaatException
from flaat.user_infos import UserInfos

logger = logging.getLogger(__name__)


def check_environment_for_override(env_key):
    """Override the actual group membership, if environment is set."""
    env_val = os.getenv(env_key)
    try:
        if env_val is not None:
            avail_entitlement_entries = json.loads(env_val)
            return avail_entitlement_entries
    except (TypeError, json.JSONDecodeError) as e:  # pragma: no cover
        logger.error(
            "Cannot decode JSON group list from the environment: %s\n%s", env_val, e
        )
    return None


@dataclass
class CheckResult:
    """CheckResult is the result of an `is_satisfied_by` check"""

    is_satisfied: bool
    message: str


class Requirement:
    """Requirement is the base class of all requirements.
    Requirement have a method `is_satisfied_by` which returns a `CheckResult` instance.
    """

    def is_satisfied_by(self, user_infos: UserInfos) -> CheckResult:
        _ = user_infos
        return CheckResult(False, "method not overwritten")


class Satisfied(Requirement):
    """Satisfied is always satisfied"""

    def is_satisfied_by(self, _):
        return CheckResult(True, "Requirement is always satisfied")


class Unsatisfiable(Requirement):
    """Unsatisfiable is never satisfied"""

    def is_satisfied_by(self, _):
        return CheckResult(False, "Requirement is unsatisfiable")


class IsTrue(Requirement):
    """IsTrue is satisfied if the provided func evaluates to True"""

    def __init__(self, func: Callable[[UserInfos], bool]):
        self.func = func

    def is_satisfied_by(self, user_infos: UserInfos) -> CheckResult:
        return CheckResult(
            self.func(user_infos), f"Evaluation of: {self.func.__name__}"
        )


class MetaRequirement(Requirement):
    """MetaRequirement is a requirements consisting of multiple sub-requirements
    Use the childs AllOf, OneOf or N_Of directly.
    """

    def __init__(self, *reqs: Requirement):
        self.requirements = list(reqs)

    def add_requirement(self, req: Requirement):
        self.requirements.append(req)


class AllOf(MetaRequirement):
    """AllOf is satisfied if all of its sub-requirements are satisfied"""

    def is_satisfied_by(self, user_infos: UserInfos) -> CheckResult:
        satisfied = True
        message = "All sub-requirements are satisfied"
        failed_messages = []

        for req in self.requirements:
            check_result = req.is_satisfied_by(user_infos)
            if not check_result.is_satisfied:
                failed_messages.append(check_result.message)
                satisfied = False

        if not satisfied:
            message = f"Unsatisfied sub-requirements: {failed_messages}"

        return CheckResult(satisfied, message)


class OneOf(MetaRequirement):
    """OneOf is satisfied if at least one of its sub-requirements are satisfied"""

    def is_satisfied_by(self, user_infos: UserInfos) -> CheckResult:
        satisfied = True
        message = "All sub-requirements are satisfied"
        failed_messages = []

        for req in self.requirements:
            check_result = req.is_satisfied_by(user_infos)
            if not check_result.is_satisfied:
                satisfied = False
                failed_messages.append(check_result.message)

        if not satisfied:
            message = f"No sub-requirements are satisfied: {failed_messages}"

        return CheckResult(satisfied, message)


class N_Of(MetaRequirement):
    """N_Of is satisfied if at least `n` of its sub requirements are satisfied"""

    def __init__(self, n: int, *reqs: Requirement):
        super().__init__(*reqs)
        self.n = n

    def is_satisfied_by(self, user_infos: UserInfos) -> CheckResult:
        failed_messages = []
        n = 0
        for req in self.requirements:
            check_result = req.is_satisfied_by(user_infos)
            if not check_result.is_satisfied:
                failed_messages.append(check_result.message)
            else:
                n += 1

        if n >= self.n:
            return CheckResult(True, f"{n} of {self.n} sub-requirments are satisfied")

        return CheckResult(
            False,
            f"Only {n} of {self.n} sub requirments were satisfied: {failed_messages}",
        )


def match_to_meta_requirement(match: Union[str, int]) -> MetaRequirement:
    """translates a match argument to meta requirements
    Valid values are: "all", "one" or int"""

    if match == "all":
        return AllOf()
    if match == "one":
        return OneOf()
    if isinstance(match, int):
        return N_Of(match)

    raise FlaatException("Argument 'match' has invalid value: Must be 'all' or int")


class HasSubIss(Requirement):
    """HasSubIss is satisfied if the user has a subject and an issuer"""

    def is_satisfied_by(self, user_infos: UserInfos) -> CheckResult:
        if user_infos is None:
            return CheckResult(False, "No valid user_infos found")

        if user_infos.subject != "" and user_infos.issuer != "":
            return CheckResult(
                True, "Valid user: {user_infos.subject} @ {user_infos.issuer}"
            )

        return CheckResult(False, "user_infos have no subject / issuer")


class HasClaim(Requirement):
    """HasClaim is satisfied if the user has the specified claim value"""

    def __init__(self, required, claim: str):
        """
        claim is the name of the claim.
        value is the value the claim needs to have
        """
        # try parsing the value, if it does not work revert to equal comparisons
        self.use_parse = True
        self.value = self.parse(required)
        if self.value is None:
            self.use_parse = False
            self.value = required

        self.claim = claim

    def _get_override_claims(self) -> Optional[Any]:
        override_entitlement_entries = check_environment_for_override(
            "DISABLE_AUTHENTICATION_AND_ASSUME_ENTITLEMENTS"
        )
        if override_entitlement_entries is not None:
            logger.info("Using entitlement override: %s", override_entitlement_entries)
            return override_entitlement_entries

        return None

    def is_satisfied_by(self, user_infos: UserInfos) -> CheckResult:
        override_claim = self._get_override_claims()
        if override_claim is not None:
            value = override_claim
        else:
            value = user_infos.get(self.claim, None)
        if value is None:
            return CheckResult(False, f"Claim '{self.claim}' is not available")

        matched = False
        matched_value = None
        if isinstance(value, list):
            for val in value:
                if self.matches(self.value, self.parse(val)):
                    matched_value = val
                    matched = True
                    break

        if not matched:
            return CheckResult(
                False,
                f"No match for the required value '{self.value}' of claim '{self.claim}'",
            )

        return CheckResult(
            True,
            f"Match for the required value '{self.value}' of claim '{self.claim}': '{matched_value}'",
        )

    def _parse(self, raw):
        """_parse can be overwritten by subclasses"""
        return raw

    def parse(self, raw):
        if self.use_parse:
            return self._parse(raw)
        return raw

    def _matches(self, required, available) -> bool:
        """_matches can be overwritten by subclasses"""
        return required == available

    def matches(self, required, available) -> bool:
        if self.use_parse:
            return self._matches(required, available)
        return required == available


class HasAARCEntitlement(HasClaim):
    """HasAARCEntitlement is satisfies if the user has the provided AARC-G002/G069 entitlement
    If the argument `required` is not a parseable AARC entitlement, we revert to equals comparisons.
    """

    def _parse(self, raw: str):
        try:
            return aarc_entitlement.G069(raw)
        except aarc_entitlement.Error as e:
            logger.debug("Error parsing aarc entitlement: %s", e)
            return None

    def _matches(
        self, required: aarc_entitlement.Base, available: aarc_entitlement.Base
    ) -> bool:
        return available.satisfies(required)


def get_claim_requirement(
    required: Union[str, List[str]],
    claim: str,  # claim in the user info
    match: Union[str, int] = "all",
    claim_requirement_class=HasClaim,
) -> Requirement:
    """get_claim_requirement returns a requirement that is satisfied if the user has the claim value(s) of `required`.
    Depending on the `match` argument all or a specific number of values are required to be matched.

    If the claim values need specific handling, claim_requirement_class can be used to specify, a class
    for the handling, see `get_vo_requirement`)
    """
    if isinstance(required, list):
        requirement = match_to_meta_requirement(match)

        for req in required:
            requirement.add_requirement(claim_requirement_class(req, claim=claim))
    else:
        requirement = claim_requirement_class(required, claim=claim)

    return requirement


def get_vo_requirement(
    required: Union[str, List[str]],
    claim: str,  # claim in the user info
    match: Union[str, int] = "all",
) -> Requirement:
    """Equivalent to `get_claim_requirement`, but works for both groups and AARC entitlements"""
    return get_claim_requirement(
        required, claim, match, claim_requirement_class=HasAARCEntitlement
    )


# REQUIREMENT is the type of requirements, either lazy or not
REQUIREMENT = Union[Requirement, Callable[[], Requirement]]

REQUEST_REQUIREMENT = Callable[[UserInfos, tuple, dict], CheckResult]
