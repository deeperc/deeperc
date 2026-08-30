from trace.writer import write_trace


def confirm_voltages(power_rails: list[dict], skip: bool = False) -> dict[str, float | None]:
    confirmed: dict[str, float | None] = {}
    for rail in power_rails:
        net = rail.get("net_name", "")
        v = rail.get("voltage_v")
        if not net:
            continue
        if v is not None:
            try:
                confirmed[net] = float(v)
            except (TypeError, ValueError):
                confirmed[net] = None
        else:
            # Rail recognized by name but voltage unknown (e.g. bare "VCC")
            confirmed[net] = None

    if skip or not confirmed:
        for net, voltage in confirmed.items():
            write_trace(
                net_name=net,
                step_id="step_07_confirm",
                step_display_name="Voltage confirmation",
                step_type="deterministic",
                decision_path="auto_skip",
                inputs={"proposed_voltage": next(
                    (r.get("voltage_v") for r in power_rails if r.get("net_name") == net), None
                )},
                outputs={"confirmed_voltage": voltage},
            )
        return confirmed

    print("\n" + "─" * 41)
    print("  POWER NET CONFIRMATION (Step 7)")
    print("─" * 41)
    print("  Inferred power rails:\n")

    items = list(confirmed.items())
    for i, (net, voltage) in enumerate(items, 1):
        rail_meta = next(
            (r for r in power_rails if r.get("net_name") == net), {}
        )
        source = rail_meta.get("source", "?")
        conf   = rail_meta.get("confidence", "?")
        if source == "deterministic":
            source_tag = "deterministic ✓"
        elif source == "gemma":
            source_tag = f"gemma — {conf} confidence"
        else:
            source_tag = source
        print(f"  [{i}] {net:<12} →  {voltage}V   ({source_tag})")

    print("\n  Enter corrections as: <number>=<voltage>")
    print("  Example: 1=3.3 2=5.0")
    raw = input("  Press Enter to accept all: ").strip()
    print("─" * 41 + "\n")

    # TODO-134 precedence: a user-map declaration (source="user_confirmed") is the
    # highest tier and must not be down-graded by an interactive correction.
    _source_by_net = {r.get("net_name"): r.get("source") for r in power_rails}
    if raw:
        for token in raw.split():
            if "=" in token:
                try:
                    idx_str, v_str = token.split("=", 1)
                    idx = int(idx_str) - 1
                    net = items[idx][0]
                    if _source_by_net.get(net) == "user_confirmed":
                        print(f"  [STEP 07] {net} is user-declared (rail map) — "
                              f"keeping {confirmed[net]}, ignoring override")
                        continue
                    confirmed[net] = float(v_str)
                except (ValueError, IndexError):
                    print(f"  [STEP 07] Ignoring unrecognised correction: {token!r}")

    for net, voltage in confirmed.items():
        write_trace(
            net_name=net,
            step_id="step_07_confirm",
            step_display_name="Voltage confirmation",
            step_type="deterministic",
            decision_path="user_confirmed",
            inputs={"proposed_voltage": next(
                (r.get("voltage_v") for r in power_rails if r.get("net_name") == net), None
            )},
            outputs={"confirmed_voltage": voltage},
        )
    return confirmed
