import pytest

from coldchain.enums import (
    REASON_STATUS,
    STATUS_ACTION,
    STATUS_SEVERITY,
    ComplianceStatus,
    DecidedAction,
    IssueType,
    PartyType,
    ReasonCode,
    Scenario,
    ShipmentStatus,
    worst_status,
)


def test_enum_values_are_their_strings() -> None:
    assert ReasonCode.DATA_GAP == "DATA_GAP"
    assert IssueType.LOGGER_NOT_ASSIGNED == "logger_not_assigned"


def test_enum_members_match_plan() -> None:
    assert len(Scenario) == 6
    assert len(IssueType) == 5
    assert len(ReasonCode) == 13
    assert [s.value for s in ComplianceStatus] == [
        "COMPLIANT",
        "COMPLIANT_WITH_WARNINGS",
        "INCONCLUSIVE",
        "NON_COMPLIANT",
    ]
    assert ShipmentStatus.QUARANTINED in ShipmentStatus


def test_party_types_include_every_actor_from_the_brief() -> None:
    assert {p.value for p in PartyType} == {
        "SUPPLIER",
        "CARRIER",
        "WAREHOUSE",
        "RECEIVER",
        "PLANNER",
        "LOGISTICS_ANALYST",
        "OPERATIONS_MANAGER",
    }


def test_mappings_cover_every_member() -> None:
    assert set(REASON_STATUS) == set(ReasonCode)
    assert set(STATUS_ACTION) == set(ComplianceStatus)
    assert set(STATUS_SEVERITY) == set(ComplianceStatus)
    assert sorted(STATUS_SEVERITY.values()) == [1, 2, 3, 4]


def test_reason_codes_declared_in_severity_order() -> None:
    severities = [
        STATUS_SEVERITY[status] if (status := REASON_STATUS[code]) else 0 for code in ReasonCode
    ]
    assert severities == sorted(severities, reverse=True)


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        ([], ComplianceStatus.COMPLIANT),
        (["CARRIER_STATUS_CONTRADICTED"], ComplianceStatus.COMPLIANT),
        (["LONG_DWELL"], ComplianceStatus.COMPLIANT_WITH_WARNINGS),
        (["NEAR_LIMIT_UNCERTAIN", "DATA_GAP"], ComplianceStatus.INCONCLUSIVE),
        (["LONG_DWELL", "CUSTODY_GAP", "ABSOLUTE_LIMIT_BREACH"], ComplianceStatus.NON_COMPLIANT),
        ([ReasonCode.LOGGER_NOT_ASSIGNED], ComplianceStatus.INCONCLUSIVE),
    ],
)
def test_worst_status(codes, expected) -> None:
    assert worst_status(codes) is expected


def test_worst_status_rejects_unknown_code() -> None:
    with pytest.raises(ValueError):
        worst_status(["NOT_A_CODE"])


def test_status_actions() -> None:
    assert STATUS_ACTION[ComplianceStatus.NON_COMPLIANT] is DecidedAction.QUARANTINE
    assert STATUS_ACTION[ComplianceStatus.INCONCLUSIVE] is DecidedAction.REVIEW
    assert STATUS_ACTION[ComplianceStatus.COMPLIANT_WITH_WARNINGS] is DecidedAction.RELEASE
