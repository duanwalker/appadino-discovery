from discovery.stages.triggers import detect_triggers, resolve_angle, select_primary_trigger


def _filing(revenue_total: int | None, officers: list[dict] | None = None, tax_year: int = 2023) -> dict:
    return {"tax_year": tax_year, "revenue_total": revenue_total, "officers": officers or []}


class TestNewEdTrigger:
    def test_fires_when_executive_name_changes(self) -> None:
        current = _filing(1_000_000, [{"name": "Jane Smith", "title": "Executive Director"}], 2023)
        previous = _filing(950_000, [{"name": "John Doe", "title": "Executive Director"}], 2022)
        triggers = detect_triggers(current, previous, revenue_floor=500_000)
        types = [t["type"] for t in triggers]
        assert "new_ed" in types

    def test_does_not_fire_when_same_executive(self) -> None:
        current = _filing(1_000_000, [{"name": "Jane Smith", "title": "President"}], 2023)
        previous = _filing(950_000, [{"name": "Jane Smith", "title": "Executive Director"}], 2022)
        triggers = detect_triggers(current, previous, revenue_floor=500_000)
        assert "new_ed" not in [t["type"] for t in triggers]

    def test_no_previous_filing_no_trigger(self) -> None:
        current = _filing(1_000_000, [{"name": "Jane Smith", "title": "Executive Director"}])
        triggers = detect_triggers(current, None, revenue_floor=500_000)
        assert "new_ed" not in [t["type"] for t in triggers]

    def test_case_and_whitespace_insensitive_name_comparison(self) -> None:
        current = _filing(1_000_000, [{"name": "  jane SMITH  ", "title": "Executive Director"}])
        previous = _filing(950_000, [{"name": "Jane Smith", "title": "Executive Director"}])
        triggers = detect_triggers(current, previous, revenue_floor=500_000)
        assert "new_ed" not in [t["type"] for t in triggers]


class TestDdDepartureTrigger:
    def test_fires_when_development_officer_disappears(self) -> None:
        current = _filing(1_000_000, [{"name": "A", "title": "Treasurer"}])
        previous = _filing(950_000, [{"name": "B", "title": "Director of Development"}])
        triggers = detect_triggers(current, previous, revenue_floor=500_000)
        assert "dd_departure" in [t["type"] for t in triggers]

    def test_does_not_fire_when_dd_still_present(self) -> None:
        current = _filing(1_000_000, [{"name": "B", "title": "VP Development"}])
        previous = _filing(950_000, [{"name": "B", "title": "Director of Development"}])
        triggers = detect_triggers(current, previous, revenue_floor=500_000)
        assert "dd_departure" not in [t["type"] for t in triggers]

    def test_does_not_fire_when_never_had_dd(self) -> None:
        current = _filing(1_000_000, [{"name": "A", "title": "Treasurer"}])
        previous = _filing(950_000, [{"name": "A", "title": "Treasurer"}])
        triggers = detect_triggers(current, previous, revenue_floor=500_000)
        assert "dd_departure" not in [t["type"] for t in triggers]


class TestTransformationalRevenueJumpTrigger:
    def test_fires_above_threshold(self) -> None:
        current = _filing(1_600_000)
        previous = _filing(1_000_000)  # +60%
        triggers = detect_triggers(current, previous, revenue_floor=500_000, transformational_jump_pct=0.50)
        assert "transformational_revenue_jump" in [t["type"] for t in triggers]

    def test_does_not_fire_below_threshold(self) -> None:
        current = _filing(1_200_000)
        previous = _filing(1_000_000)  # +20%
        triggers = detect_triggers(current, previous, revenue_floor=500_000, transformational_jump_pct=0.50)
        assert "transformational_revenue_jump" not in [t["type"] for t in triggers]

    def test_handles_zero_previous_revenue_without_crash(self) -> None:
        current = _filing(1_000_000)
        previous = _filing(0)
        triggers = detect_triggers(current, previous, revenue_floor=500_000)
        assert "transformational_revenue_jump" not in [t["type"] for t in triggers]


class TestFirstFilingAboveFloorTrigger:
    def test_fires_with_no_prior_filing(self) -> None:
        current = _filing(600_000)
        triggers = detect_triggers(current, None, revenue_floor=500_000)
        assert "first_filing_above_floor" in [t["type"] for t in triggers]

    def test_fires_when_prior_was_below_floor(self) -> None:
        current = _filing(600_000)
        previous = _filing(400_000)
        triggers = detect_triggers(current, previous, revenue_floor=500_000)
        assert "first_filing_above_floor" in [t["type"] for t in triggers]

    def test_does_not_fire_when_prior_already_above_floor(self) -> None:
        current = _filing(600_000)
        previous = _filing(550_000)
        triggers = detect_triggers(current, previous, revenue_floor=500_000)
        assert "first_filing_above_floor" not in [t["type"] for t in triggers]

    def test_does_not_fire_when_current_below_floor(self) -> None:
        current = _filing(400_000)
        triggers = detect_triggers(current, None, revenue_floor=500_000)
        assert "first_filing_above_floor" not in [t["type"] for t in triggers]


class TestMultipleTriggersAndPriority:
    def test_multiple_triggers_all_detected(self) -> None:
        current = _filing(1_600_000, [{"name": "New Exec", "title": "Executive Director"}])
        previous = _filing(1_000_000, [{"name": "Old Exec", "title": "Executive Director"}])
        triggers = detect_triggers(current, previous, revenue_floor=500_000, transformational_jump_pct=0.50)
        types = {t["type"] for t in triggers}
        assert {"new_ed", "transformational_revenue_jump"}.issubset(types)

    def test_select_primary_trigger_follows_priority_order(self) -> None:
        triggers = [
            {"type": "transformational_revenue_jump", "evidence": {}},
            {"type": "new_ed", "evidence": {}},
        ]
        primary = select_primary_trigger(triggers)
        assert primary is not None
        assert primary["type"] == "new_ed"

    def test_select_primary_trigger_none_when_empty(self) -> None:
        assert select_primary_trigger([]) is None


class TestResolveAngle:
    def test_resolves_configured_angle(self) -> None:
        trigger_angles = {"new_ed": "first-100-days", "dd_departure": "Fractional/Interim DD"}
        assert resolve_angle("new_ed", trigger_angles) == "first-100-days"

    def test_returns_none_for_unmapped_trigger_rather_than_inventing_text(self) -> None:
        """first_filing_above_floor has no angle in the brief's table — per Duan's
        explicit instruction, this stays null, not a fabricated default."""
        trigger_angles = {"new_ed": "first-100-days"}
        assert resolve_angle("first_filing_above_floor", trigger_angles) is None
