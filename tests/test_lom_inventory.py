"""Official docs inventory parsing and truthful evidence labels."""

from scripts.sync_lom_inventory import check, parse_page


def test_parser_separates_access_from_functions_and_skips_footer():
    source = """<h1>Clip</h1><h2 id="properties">Properties</h2>
    <h3 id="name">name <span>symbol</span></h3>
    <p>Access: <span>get</span><span>set</span><span>observe</span></p>
    <h2 id="functions">Functions</h2><h3 id="apply_note_modifications">apply_note_modifications</h3>
    <p>Available since Live 11.0.</p><h3 class="footer">Resources</h3>"""
    result = parse_page("https://docs.cycling74.com/apiref/lom/clip/", source)
    assert len(result["members"]) == 2
    assert result["members"][0]["access"] == ["get", "set", "observe"]
    assert result["members"][1]["since_live"] == "11.0"


def test_inventory_is_complete_within_its_snapshot_without_claiming_live_acceptance():
    result = check()
    assert result["object_count"] >= 30
    assert result["member_count"] >= 400
    for obj in result["objects"]:
        for member in obj["members"]:
            assert member["coverage"]["runtime_acceptance"] == "not_run"
