"""Everything the web UI shows, as one JSON-ready dict (``GET /api/state``).

Times are UNIX seconds (floats); the page formats them relative to "now"
(``now`` in the payload, so a slow phone clock doesn't skew "3 min ago").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ..hub import Hub

Level = Literal["ok", "warn", "problem"]


@dataclass(frozen=True, slots=True)
class Issue:
    """One thing the health banner can say, most important first."""

    level: Level
    title: str
    detail: str
    #: What the banner's button does: a tab to open, or an action to run.
    action: str | None = None
    action_label: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON form."""
        return {
            "level": self.level,
            "title": self.title,
            "detail": self.detail,
            "action": self.action,
            "action_label": self.action_label,
        }


def issues_for(state: dict[str, Any]) -> list[Issue]:
    """Plain-language problems in priority order (empty: all good).

    ``state`` is the payload built by :func:`build_state` (without
    ``health``), so this stays a pure function that tests can feed directly.
    """
    found: list[Issue] = []
    reader = state["reader"]
    grocy = state["grocy"]
    door = state["door"]
    if reader["config_error"]:
        found.append(
            Issue(
                "problem",
                "Reader antenna settings could not be applied",
                f"{reader['config_error']}. Check that every enabled port has an "
                "antenna connected. Retrying automatically.",
            )
        )
    elif reader["online"] is False:
        found.append(
            Issue(
                "problem",
                "Reader offline",
                f"Check it’s powered and on the network ({reader['host']}).",
            )
        )
    elif reader["online"] is None:
        found.append(
            Issue(
                "warn",
                "Connecting to the reader…",
                f"Waiting for the first answer from {reader['host']}.",
            )
        )
    if grocy["online"] is False:
        detail = grocy["last_error"] or "No answer"
        found.append(
            Issue(
                "problem",
                "Can’t reach Grocy",
                f"{detail}. Changes are queued and sent when it’s back.",
            )
        )
    if grocy["failed"]:
        n = grocy["failed"]
        found.append(
            Issue(
                "warn",
                f"{n} Grocy update{'s' if n != 1 else ''} failed",
                f"Open Tags to retry or dismiss {'them' if n != 1 else 'it'}.",
                "tags",
                "Retry",
            )
        )
    if state["mqtt"]["enabled"] and state["mqtt"]["connected"] is False:
        found.append(
            Issue(
                "warn",
                "MQTT disconnected",
                "The linking station and Home Assistant entities can’t update "
                "until it reconnects.",
            )
        )
    if door["needed"]:
        found.append(
            Issue(
                "warn",
                "No door sensor chosen",
                "Pick the pantry door sensor in Settings. Until then Pantry Hub "
                "checks once an hour.",
                "settings",
                "Choose",
            )
        )
    elif door["missing"]:
        found.append(
            Issue(
                "warn",
                "Door sensor not found",
                f"{door['entity_id']} isn’t in Home Assistant (renamed or "
                "deleted?). Pick it again in Settings.",
                "settings",
                "Fix",
            )
        )
    reading = state.get("reading") or {}
    if reading.get("mode") == "continuous" and not door["needed"]:
        # 0.4.5: continuous reading is a test mode; say so instead of running
        # silently without the door sensor.
        no_door = not door.get("entity_id")
        found.append(
            Issue(
                "warn",
                "No door sensor: reading continuously (test mode)"
                if no_door
                else "Reading continuously (test mode)",
                (
                    "Pantry Hub reads the pantry all the time instead of when "
                    "the door opens. Pair the door sensor, pick it in Settings, "
                    "then set reading_mode to door in the app's Configuration."
                )
                if no_door
                else (
                    "Pantry Hub reads the pantry all the time instead of when "
                    "the door opens. Set reading_mode to door in the app's "
                    "Configuration to use the door sensor."
                ),
                "settings",
                "Settings",
            )
        )
    pictures = state.get("pictures") or {}
    if pictures.get("auth_failed"):
        found.append(
            Issue(
                "warn",
                "Kroger Restock needs you to sign in again",
                "Product pictures are paused. Sign in to Kroger again in Home "
                "Assistant (Kroger Restock), then tap Fetch missing pictures.",
                "settings",
                "Open",
            )
        )
    for port in reader["dead_ports"]:
        found.append(
            Issue(
                "warn",
                f"Antenna on port {port} gave no reads",
                "No reads in 5 checks — check its cable and connection.",
            )
        )
    return found


def health_for(state: dict[str, Any]) -> dict[str, Any]:
    """Return the banner (most important issue) plus every issue."""
    found = issues_for(state)
    if not found:
        top = Issue("ok", "All good", "Reader, Grocy and MQTT are working.")
    else:
        top = found[0]
    return {**top.as_dict(), "issues": [i.as_dict() for i in found]}


async def build_state(hub: Hub) -> dict[str, Any]:
    """Collect everything the page shows (one consistent snapshot)."""
    await hub.refresh_counts()
    s = hub.state()
    reading = s["reading"]
    opts = hub.options
    store = hub.store
    links = await store.links()
    unlinked = await store.unlinked()
    rows = {r.epc: r for r in await store.presence_rows()}
    failed = await store.ops("failed", limit=100)
    needs = await store.needs_product()
    in_pantry = set(hub.machine.in_pantry())
    by_product: dict[int, dict[str, Any]] = {}
    for link in links:
        if link.epc not in in_pantry:
            continue
        row = rows.get(link.epc)
        since = row.first_seen if row else None
        item = by_product.setdefault(
            link.product_id,
            {
                "product_id": link.product_id,
                "name": link.product_name,
                "quantity": 0,
                "since": since,
                "epcs": [],
            },
        )
        item["quantity"] += 1
        item["epcs"].append(link.epc)
        if since is not None and (item["since"] is None or since < item["since"]):
            item["since"] = since
    items = sorted(by_product.values(), key=lambda i: i["name"].casefold())
    linked_epcs = {link.epc for link in links}
    mqtt = hub.mqtt
    source = hub.door_source
    dead = hub.dead_ports
    last = hub.last_sweep_attempt
    state: dict[str, Any] = {
        "version": hub.version,
        "now": hub.wall(),
        "reader": {
            "host": opts.reader_host,
            # Door mode: None until the first window or probe reached it.
            "online": hub.session.online if hub.door_mode else s["reader_online"],
            "activity": s["activity"],
            "connection": reading["reader_connection"],
            "antennas_enabled": reading["antennas_enabled"],
            "power_dbm": reading["reader_power_dbm"],
            "config_error": reading["last_config_error"],
            "error": reading["reader_error"],
            "dead_ports": reading["dead_ports"],
            "per_port_reads_last_sweep": reading["per_port_reads_last_sweep"],
            "reader_id": reading["reader_id"],
        },
        "grocy": {
            "online": s["grocy"]["online"],
            "queue": s["grocy_queue"],
            "failed": s["grocy"]["failed"],
            "review": s["grocy"]["review"],
            "last_error": s["grocy"]["last_error"],
            "ui_url": opts.grocy_browser_url,
        },
        "mqtt": {
            "enabled": mqtt is not None,
            "connected": mqtt.connected if mqtt is not None else None,
        },
        "door": {
            "entity_id": hub.door_entity,
            "web_entity_id": hub.door_entity_web,
            "source": hub.door_entity_source,
            "state": hub.machine.door.value,
            "raw": source.raw_state if source else None,
            "inverted": hub.door_inverted,
            "needed": hub.door_sensor_needed,
            "missing": bool(source and source.missing),
            "ha_available": hub.ha_token is not None,
        },
        "reading": {
            "mode": opts.reading_mode,
            "activity": s["activity"],
            "last_sweep_at": last["epoch"] if last else None,
            "last_sweep_completed": last["completed"] if last else None,
            "last_completed_sweep_at": hub.last_sweep["epoch"]
            if hub.last_sweep
            else None,
            "next_sweep_at": hub.next_sweep_at(),
            "sweeps_completed": reading["sweeps_completed"],
            "sweeps_aborted": reading["sweeps_aborted"],
            "departures_allowed": reading["departures_allowed"],
        },
        "allowances": await _allowances(hub),
        "pantry": {
            "items": items,
            "products": len(items),
            "linked_present": sum(i["quantity"] for i in items),
            "present_total": len(in_pantry),
            "unlinked_present": s["unlinked_tags"],
        },
        "unlinked": [
            {
                "epc": u.epc,
                "first_seen": u.first_seen,
                "last_seen": u.last_seen,
                "arrivals": u.arrivals,
                "present": u.epc in in_pantry,
            }
            for u in unlinked
            if u.epc not in linked_epcs
        ],
        "links": [
            {
                "epc": lk.epc,
                "product_id": lk.product_id,
                "product_name": lk.product_name,
                "linked_at": lk.linked_at,
                "last_seen": rows[lk.epc].last_seen if lk.epc in rows else None,
                "present": lk.epc in in_pantry,
                "skip_add_cycle": lk.skip_add_cycle,
            }
            for lk in sorted(links, key=lambda x: x.product_name.casefold())
        ],
        "failed_ops": [
            {
                "id": op.id,
                "kind": op.kind,
                "product": op.product_name,
                "epc": op.epc,
                "error": op.last_error,
                "needs_review": op.needs_review,
            }
            for op in failed
        ],
        "needs_product": [
            {
                "barcode": n.barcode,
                "station": n.station,
                "last_seen": n.last_seen,
                "scans": n.scans,
            }
            for n in needs
        ],
        "events": list(reversed(hub.recent_events)),
        "pictures": _pictures(hub),
        "just_bought": await _just_bought(hub),
        "scan": _scan(hub),
        "stocktake": await hub.scans.state() if hub.scans else None,
        "grocy_untagged": _grocy_untagged(hub),
        "resync": _resync(hub),
        "options": {
            "reading_mode": opts.reading_mode,
            "reader_antennas": list(opts.reader_antennas),
            "reader_power_dbm": opts.reader_power_dbm,
            "max_reader_power_dbm": opts.max_reader_power_dbm,
            "reader_connection": opts.reader_connection,
        },
        "diagnostics": _diagnostics(hub, s, dead is not None),
    }
    # Details: each product still waiting for tags of units already in Grocy.
    state["diagnostics"] += [[a["name"], a["text"]] for a in state["allowances"]]
    state["health"] = health_for(state)
    return state


def _resync(hub: Hub) -> dict[str, Any] | None:
    report = hub.last_resync
    if report is None:
        return None
    return {
        "checked_at": report.checked_at,
        "checked": report.checked,
        "differences": report.differences,
        "error": report.error,
        "review_ops": report.review_ops,
    }


def _pictures(hub: Hub) -> dict[str, Any]:
    """Kroger Restock product pictures: availability + backfill progress."""
    pics = hub.pictures
    if pics is None:
        return {
            "available": False,
            "running": False,
            "text": None,
            "versions": {},
            "auth_failed": False,
            "status": "Kroger Restock not found.",
        }
    p = pics.progress
    started = p.running or p.finished_at is not None
    return {
        "available": pics.available,
        "running": p.running,
        "text": p.text() if started else None,
        "versions": {str(k): v for k, v in pics.versions.items()},
        "auth_failed": pics.auth_failed,
        "status": pics.status_line(),
    }


def allowance_text(units: int, sources: list[str]) -> str:
    """``3 counted units still to tag`` (``booked`` for Not tagging rows)."""
    counted = all(s in ("stocktake", "grocy") for s in sources)
    word = "counted" if counted else "booked"
    return f"{units} {word} unit{'s' if units != 1 else ''} still to tag"


async def _allowances(hub: Hub) -> list[dict[str, Any]]:
    """Products whose next tagged units are already in Grocy (7-day allowance)."""
    found = await hub.store.allowances(hub.wall())
    if not found:
        return []
    names = {lk.product_id: lk.product_name for lk in await hub.store.links()}
    gu = hub.grocy_untagged
    for row in gu.rows if gu is not None else []:
        names.setdefault(int(row["product_id"]), str(row["name"]))
    for r in await hub.store.received(include_done=True):
        if r.grocy_product_id is not None:
            names.setdefault(r.grocy_product_id, r.name)
    if hub.scans is not None:
        for review in hub.scans.stocktake.review:
            names.setdefault(review.product_id, review.name)
    return sorted(
        (
            {
                "product_id": pid,
                "name": names.get(pid, f"Product #{pid}"),
                "units": a["units"],
                "sources": a["sources"],
                "expires": a["expires"],
                "text": allowance_text(a["units"], a["sources"]),
            }
            for pid, a in found.items()
        ),
        key=lambda r: str(r["name"]).casefold(),
    )


def _scan(hub: Hub) -> dict[str, Any]:
    """Unload / Stock-take session and the last scan's answer."""
    scans = hub.scans
    if scans is None:
        return {"available": False, "session": None, "last": None}
    last = scans.last_result
    return {
        "available": True,
        "session": scans.session.as_dict() if scans.session else None,
        "last": last.payload() if last else None,
        "station": hub.tag_station(),
    }


def _grocy_untagged(hub: Hub) -> dict[str, Any]:
    """Pantry stock in Grocy without tags (refreshed in the background)."""
    gu = hub.grocy_untagged
    if gu is None:
        return {"rows": [], "error": None}
    gu.refresh_soon()
    return {"rows": gu.rows, "error": gu.error}


async def _just_bought(hub: Hub) -> dict[str, Any]:
    """Kroger deliveries to tag, plus the running counted session."""
    jb = hub.just_bought
    counted = hub.linking.counted
    session = None
    if counted is not None and (
        not counted.ended or hub.linking.active(counted.station)
    ):
        session = counted.progress()
    if jb is None:
        return {
            "available": False,
            "rows": [],
            "to_tag": 0,
            "session": session,
            "untracked": None,
        }
    rows = await jb.rows()
    return {
        "available": jb.available is True,
        "untracked": await jb.untracked(),
        "rows": rows,
        "to_tag": sum(r["to_tag"] for r in rows if not r["older"]),
        "station": hub.tag_station(),
        "session": session,
    }


def _diagnostics(hub: Hub, s: dict[str, Any], door_mode: bool) -> list[list[str]]:
    """Key/value rows for the banner's Details (strings, already formatted)."""
    r = s["reading"]
    rd = s["reader"]
    mqtt = hub.mqtt
    rows: list[list[str]] = [
        ["Reader", f"{hub.options.reader_host}:{hub.options.reader_tcp_port}"],
        ["Reader online", _yn(s["reader_online"])],
        ["Reader id", str(r["reader_id"])],
        ["Reading mode", hub.options.reading_mode],
        ["Activity", s["activity"]],
        ["Grocy", _yn(s["grocy"]["online"], unknown="not used yet")],
        ["Grocy queue", str(s["grocy_queue"])],
        ["MQTT", _yn(mqtt.connected if mqtt else None, unknown="off")],
    ]
    pics = hub.pictures
    line = "Kroger Restock not found." if pics is None else pics.status_line()
    if line:
        rows.append(["Product pictures", line])
    elif pics is not None and pics.available:
        rows.append(["Product pictures", "from Kroger Restock"])
    if door_mode:
        rows += [
            ["Connection", str(r["reader_connection"])],
            ["Antennas", ", ".join(map(str, r["antennas_enabled"] or [])) or "—"],
            ["Power", f"{r['reader_power_dbm']} dBm" if r["reader_power_dbm"] else "—"],
            ["Sweeps", f"{r['sweeps_completed']} done, {r['sweeps_aborted']} aborted"],
            [
                "Reads per port (last sweep)",
                ", ".join(
                    f"{p}: {n}"
                    for p, n in (r["per_port_reads_last_sweep"] or {}).items()
                )
                or "—",
            ],
            ["Dead ports", ", ".join(map(str, r["dead_ports"])) or "none"],
            [
                "Departures",
                "allowed" if r["departures_allowed"] else "paused (restart)",
            ],
        ]
        if r["reader_error"]:
            rows.append(["Last reader error", str(r["reader_error"])])
        if r["last_config_error"]:
            rows.append(["Antenna settings error", str(r["last_config_error"])])
    else:
        rows += [
            ["Last datagram", str(rd["last_datagram"] or "never")],
            ["Datagrams", str(rd["datagrams"])],
            ["Bad frames", str(rd["bad_frames"])],
            ["Dropped sources", str(rd["dropped_sources"])],
        ]
    if s["grocy"]["last_error"]:
        rows.append(["Last Grocy error", str(s["grocy"]["last_error"])])
    return rows


def _yn(value: bool | None, *, unknown: str = "unknown") -> str:
    if value is None:
        return unknown
    return "yes" if value else "no"
