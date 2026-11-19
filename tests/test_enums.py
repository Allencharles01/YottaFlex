from coldchain.enums import ComplianceStatus, IssueType, ReasonCode, Scenario, ShipmentStatus


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
