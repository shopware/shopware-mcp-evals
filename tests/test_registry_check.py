"""The registry cross-check.

The value of this module is catching a tool wrongly filed as READ_ONLY, because
that is the one classification mistake that executes something for real. So the
tests care most about it noticing, and about it not crying wolf.
"""

from eval import registry_check as rc

# A verbatim slice of `bin/console debug:mcp --tools --no-ansi`, including the
# header and rule lines, so the parser is tested against the real shape.
REAL_OUTPUT = """Tools (30)
----------

+----------------------------+--------------------+----------------+------+--------------------------+
| Name                       | Group              | Source         | Deps | Privileges               |
+----------------------------+--------------------+----------------+------+--------------------------+
| merchant-bestseller-report | merchant-analytics | Swag\\Bestseller |      | order:read, product:read |
| shopware-entity-search     | entity             | Sw\\SearchTool  | sch  | <entity>:read            |
| shopware-entity-upsert     | entity             | Sw\\UpsertTool  | sch  | <entity>:create          |
| shopware-entity-schema     | entity             | Sw\\SchemaTool  |      |                          |
+----------------------------+--------------------+----------------+------+--------------------------+
"""


def test_it_parses_the_real_table_and_ignores_the_rules_and_header() -> None:
    tools = rc.parse_tools(REAL_OUTPUT)

    assert set(tools) == {
        "merchant-bestseller-report",
        "shopware-entity-search",
        "shopware-entity-upsert",
        "shopware-entity-schema",
    }
    assert tools["shopware-entity-upsert"] == "<entity>:create"
    assert tools["shopware-entity-schema"] == ""


def test_the_real_table_is_clean() -> None:
    """These four are classified correctly today, so the check must stay quiet.
    A check that fires on correct input gets switched off."""
    assert rc.problems(rc.parse_tools(REAL_OUTPUT)) == []


def test_a_read_only_tool_needing_write_privileges_is_caught() -> None:
    """The failure this exists for: it would be executed for real, no dryRun."""
    found = rc.problems({"shopware-entity-search": "<entity>:read, <entity>:update"})

    assert len(found) == 1
    assert "READ_ONLY" in found[0] and "no dryRun" in found[0]


def test_an_unregistered_classification_is_not_this_check_s_business() -> None:
    """toolclass may name tools this instance does not have (a plugin absent).
    That is not a safety problem and must not fail the build."""
    assert rc.problems({}) == []


def test_a_tool_the_server_grew_is_flagged_as_unclassified() -> None:
    found = rc.problems({"shopware-brand-new-tool": "brand:delete"})

    assert len(found) == 1
    assert "unclassified" in found[0]


def test_empty_privileges_never_read_as_mutating() -> None:
    """A read tool with no ACL at all is the common case, not a finding."""
    assert not rc.mutates("")
    assert not rc.mutates(None)
    assert not rc.mutates("order:read, product:read")


def test_every_mutating_verb_is_detected() -> None:
    for verb in rc.MUTATING_PRIVILEGES:
        assert rc.mutates(f"thing:{verb}"), verb


def test_junk_input_parses_to_nothing_rather_than_exploding() -> None:
    """`debug:mcp` printing a help page or an error must be a visible zero — main()
    turns that into a failure — not a traceback or a silent pass."""
    assert rc.parse_tools("Command 'debug:mcp' is not defined.") == {}
    assert rc.parse_tools("") == {}
