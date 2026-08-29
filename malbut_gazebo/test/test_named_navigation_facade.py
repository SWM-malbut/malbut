"""Contracts for the named-target façade above local Robot Web."""

from copy import deepcopy

import pytest

from malbut_gazebo.named_navigation import (
    NamedNavigationError,
    parse_named_navigation_catalog,
)
from malbut_gazebo.named_navigation_facade import (
    NamedNavigationFacade,
    NamedNavigationFacadeError,
    NamedNavigationExecution,
    PreparedNamedNavigation,
    SimulationNavigationAuthority,
    terminal_status_dict,
)
from malbut_gazebo.robot_web_navigation_client import (
    CancelResult,
    EditorConfig,
    NavigationPreview,
    NavigationSession,
    NavigationStatus,
    RobotWebOutcomeUnknown,
)


DEVICE_ID = "malbut-sim-01"
MAP_ID = "map-test"
MAP_REVISION = "rev-test"


def _user_map(*, name="거실", point=(1.0, 1.0), revision=MAP_REVISION):
    return {
        "type": "FeatureCollection",
        "format": "malbut-user-map-v1",
        "map_id": MAP_ID,
        "map_revision": revision,
        "frame_id": "map",
        "room_segmentation": {"room_count": 1},
        "features": [{
            "type": "Feature",
            "id": "room-1",
            "properties": {
                "role": "room",
                "room_id": "room-1",
                "name": name,
                "category": "living_room",
                "area_m2": 16.0,
                "representative_point": list(point),
                "clearance_m": 1.0,
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [0.0, 0.0],
                    [4.0, 0.0],
                    [4.0, 4.0],
                    [0.0, 4.0],
                    [0.0, 0.0],
                ]],
            },
        }],
    }


def _catalog(value=None):
    return parse_named_navigation_catalog(
        _user_map() if value is None else value,
        device_id=DEVICE_ID,
        expected_map_id=MAP_ID,
        expected_map_revision=MAP_REVISION,
    )


class FakeClient:
    """Record façade calls while keeping capabilities opaque."""

    def __init__(
        self,
        *,
        map_id=MAP_ID,
        revision=MAP_REVISION,
        device_id=DEVICE_ID,
        simulation=True,
    ):
        """Create a successful fake with an isolated opaque owner."""
        self.calls = []
        self.config = EditorConfig(
            map_id,
            revision,
            True,
            "a" * 64,
            device_id,
            simulation,
        )
        self.owner = object()
        self.session = None

    def bootstrap(self):
        """Return the configured map identity."""
        self.calls.append(("bootstrap",))
        return self.config

    def preview(
        self,
        *,
        map_id,
        map_revision,
        x,
        y,
        user_map_digest,
        target_binding_digest,
    ):
        """Record the one private coordinate projection."""
        self.calls.append((
            "preview",
            map_id,
            map_revision,
            x,
            y,
            user_map_digest,
            target_binding_digest,
        ))
        return NavigationPreview(
            "private-preview",
            self.owner,
            30.0,
            target_binding_digest,
        )

    def start(self, preview):
        """Record one preview consumption."""
        self.calls.append(("start", preview))
        self.session = NavigationSession(
            "private-session",
            self.owner,
            "driving",
            preview._target_binding_digest,
        )
        return self.session

    def status_for(self, session):
        """Return one terminal observation for the exact session."""
        assert session is self.session
        self.calls.append(("status_for", session))
        return NavigationStatus("succeeded", self.session, 1.0)

    def cancel(self, session):
        """Record one exact-session cancellation."""
        self.calls.append(("cancel", session))
        return CancelResult("canceling", False)


def _authority():
    return SimulationNavigationAuthority.explicit_test_authority()


def test_preview_accepts_only_name_and_redacts_private_target_data():
    """Translate the semantic name privately and never start by default."""
    client = FakeClient()
    facade = NamedNavigationFacade(lambda: _catalog(), client)

    prepared = facade.preview("  거실  ")

    assert [call[0] for call in client.calls] == ["bootstrap", "preview"]
    assert client.calls[-1][3:5] == (1.0, 1.0)
    assert len(client.calls[-1][5]) == 64
    assert len(client.calls[-1][6]) == 64
    public = prepared.to_public_dict()
    serialized = repr(public)
    assert public["state"] == "previewed"
    assert public["physical_authorized"] is False
    assert not {"device_id", "room_id", "x", "y"} & set(public["target"])
    for private in (
        "private-preview",
        "malbut-sim-01",
        client.calls[-1][5],
        client.calls[-1][6],
    ):
        assert private not in serialized


def test_default_facade_refuses_start_without_consuming_preview():
    """Make preview-only the default even after a valid path was computed."""
    client = FakeClient()
    facade = NamedNavigationFacade(lambda: _catalog(), client)
    prepared = facade.preview("거실")

    with pytest.raises(
        NamedNavigationFacadeError,
        match="preview-only",
    ) as caught:
        facade.start(prepared)

    assert caught.value.code == "simulation_authority_required"
    assert [call[0] for call in client.calls].count("start") == 0


def test_explicit_simulation_navigation_previews_revalidates_and_starts_once():
    """Use one private coordinate and consume one preview exactly once."""
    client = FakeClient()
    loads = []

    def load():
        loads.append("load")
        return _catalog()

    facade = NamedNavigationFacade(load, client, authority=_authority())
    execution = facade.navigate("거실")

    assert len(loads) == 2
    assert [call[0] for call in client.calls] == [
        "bootstrap",
        "preview",
        "bootstrap",
        "start",
    ]
    assert execution.to_public_dict()["state"] == "driving"
    assert "private-session" not in repr(execution)
    assert "private-preview" not in repr(execution)


def test_invalid_session_after_start_is_unknown_not_rejected():
    """Treat a malformed post-accept handle as an ambiguous robot effect."""

    class MismatchedSessionClient(FakeClient):
        def start(self, preview):
            self.calls.append(("start", preview))
            return NavigationSession(
                "private-session",
                self.owner,
                "driving",
                "f" * 64,
            )

    client = MismatchedSessionClient()
    facade = NamedNavigationFacade(
        lambda: _catalog(), client, authority=_authority()
    )
    prepared = facade.preview("거실")

    with pytest.raises(RobotWebOutcomeUnknown) as caught:
        facade.start(prepared)

    assert caught.value.operation == "start"
    assert caught.value.cause_code == "INVALID_ACCEPTED_SESSION"
    assert [call[0] for call in client.calls].count("start") == 1


@pytest.mark.parametrize("mutation", ["name", "point", "revision"])
def test_change_after_preview_invalidates_target_without_start(mutation):
    """Bind name, coordinate, and revision through the pre-start reload."""
    client = FakeClient()
    first = _user_map()
    second = deepcopy(first)
    if mutation == "name":
        second["features"][0]["properties"]["name"] = "주방"
    elif mutation == "point":
        second["features"][0]["properties"][
            "representative_point"
        ] = [2.0, 2.0]
    else:
        second["map_revision"] = "rev-new"
    values = iter((first, second))

    def load():
        value = next(values)
        return parse_named_navigation_catalog(
            value,
            device_id=DEVICE_ID,
            expected_map_id=MAP_ID,
            expected_map_revision=(
                MAP_REVISION
                if mutation != "revision"
                else value["map_revision"]
            ),
        )

    facade = NamedNavigationFacade(load, client, authority=_authority())
    prepared = facade.preview("거실")

    with pytest.raises(NamedNavigationFacadeError) as caught:
        facade.start(prepared)

    assert caught.value.code == "target_binding_changed"
    assert [call[0] for call in client.calls].count("start") == 0


def test_raw_user_map_change_after_preview_invalidates_without_start():
    """Bind the exact User Map bytes even when semantics stay equivalent."""
    client = FakeClient()
    digests = iter(("1" * 64, "2" * 64))

    def load():
        return parse_named_navigation_catalog(
            _user_map(),
            device_id=DEVICE_ID,
            expected_map_id=MAP_ID,
            expected_map_revision=MAP_REVISION,
            source_digest=next(digests),
        )

    facade = NamedNavigationFacade(load, client, authority=_authority())
    prepared = facade.preview("거실")

    with pytest.raises(NamedNavigationFacadeError) as caught:
        facade.start(prepared)

    assert caught.value.code == "target_binding_changed"
    assert [call[0] for call in client.calls].count("start") == 0


def test_unknown_or_ambiguous_name_has_zero_connector_calls():
    """Fail before bootstrap, preview, or start for unresolved semantics."""
    client = FakeClient()
    facade = NamedNavigationFacade(
        lambda: _catalog(),
        client,
        authority=_authority(),
    )
    with pytest.raises(NamedNavigationError) as unknown:
        facade.navigate("서재")
    assert unknown.value.code == "target_not_found"
    assert client.calls == []

    duplicated = _user_map()
    other = deepcopy(duplicated["features"][0])
    other["id"] = "room-2"
    other["properties"]["room_id"] = "room-2"
    other["geometry"]["coordinates"][0] = [
        [5.0, 0.0],
        [9.0, 0.0],
        [9.0, 4.0],
        [5.0, 4.0],
        [5.0, 0.0],
    ]
    other["properties"]["representative_point"] = [6.0, 1.0]
    duplicated["features"].append(other)
    duplicated["room_segmentation"]["room_count"] = 2
    ambiguous_catalog = _catalog(duplicated)
    ambiguous = NamedNavigationFacade(
        lambda: ambiguous_catalog,
        client,
        authority=_authority(),
    )
    with pytest.raises(NamedNavigationError) as caught:
        ambiguous.navigate("거실")
    assert caught.value.code == "target_ambiguous"
    assert client.calls == []


def test_map_mismatch_stops_before_preview_or_start():
    """Reject a stale Robot Web map even when the semantic name is valid."""
    client = FakeClient(revision="rev-stale")
    facade = NamedNavigationFacade(
        lambda: _catalog(), client, authority=_authority()
    )

    with pytest.raises(NamedNavigationFacadeError) as caught:
        facade.navigate("거실")

    assert caught.value.code == "map_binding_changed"
    assert [call[0] for call in client.calls] == ["bootstrap"]


@pytest.mark.parametrize(
    ("client", "expected_code"),
    (
        (FakeClient(device_id="another-robot"), "device_binding_changed"),
        (FakeClient(simulation=False), "simulation_runtime_required"),
    ),
)
def test_server_owned_runtime_identity_is_required_before_preview(
    client,
    expected_code,
):
    """Do not let a same-map loopback server imply simulation authority."""
    facade = NamedNavigationFacade(
        lambda: _catalog(), client, authority=_authority()
    )

    with pytest.raises(NamedNavigationFacadeError) as caught:
        facade.navigate("거실")

    assert caught.value.code == expected_code
    assert [call[0] for call in client.calls] == ["bootstrap"]


def test_preview_cannot_be_cross_paired_with_another_semantic_target():
    """Bind an opaque preview to the exact resolved target that created it."""
    first_catalog = _catalog()
    second_value = _user_map(name="주방", point=(2.0, 2.0))
    second_catalog = _catalog(second_value)
    client = FakeClient()
    first_target = first_catalog.resolve("거실")
    second_target = second_catalog.resolve("주방")
    preview = NavigationPreview(
        "private-preview",
        client.owner,
        30.0,
        first_target.binding_digest,
    )

    with pytest.raises(NamedNavigationFacadeError) as caught:
        PreparedNamedNavigation("주방", second_target, preview)

    assert caught.value.code == "preview_binding_mismatch"


def test_session_cannot_be_cross_paired_with_another_semantic_target():
    """Bind an opaque execution session to its one semantic target."""
    first_catalog = _catalog()
    second_value = _user_map(name="주방", point=(2.0, 2.0))
    second_catalog = _catalog(second_value)
    first_target = first_catalog.resolve("거실")
    second_target = second_catalog.resolve("주방")
    session = NavigationSession(
        "private-session",
        object(),
        "driving",
        first_target.binding_digest,
    )

    with pytest.raises(NamedNavigationFacadeError) as caught:
        NamedNavigationExecution(second_target, session)

    assert caught.value.code == "session_binding_mismatch"


def test_status_and_cancel_use_only_the_existing_opaque_session():
    """Observe and cancel without constructing a new goal or identifier."""
    client = FakeClient()
    facade = NamedNavigationFacade(
        lambda: _catalog(), client, authority=_authority()
    )
    execution = facade.navigate("거실")

    status = facade.status(execution)
    result = facade.cancel(execution)
    public = terminal_status_dict(execution, status)

    assert status.terminal is True
    assert public["state"] == "succeeded"
    assert public["physical_authorized"] is False
    assert result.state == "canceling"
    assert client.calls[-2:] == [
        ("status_for", client.session),
        ("cancel", client.session),
    ]
