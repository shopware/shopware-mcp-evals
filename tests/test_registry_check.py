"""The registry cross-check.

The value of this module is catching a tool wrongly filed as READ_ONLY, because
that is the one classification mistake that executes something for real. So the
tests care most about it noticing, and about it not crying wolf.
"""

from pathlib import Path

import pytest

from eval import registry_check as rc

# A slice of `bin/console debug:mcp --tools --no-ansi` from a Shopware before
# shopware/shopware#18848: one unlabelled admin table, with the header and rule
# lines, so the parser is tested against the real shape.
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


# The shape since #18848, as DebugMcpCommand renders it: a title per server, a
# `Tools (N) [<server>]` section with the same table, and other sections after
# it. The Prompts table has enough columns and a hyphenated name to pass for a
# tool row, which is exactly what the parser must not do.
BOTH_SCOPES = """
Admin API (/api/_mcp)
=====================

Tools (2) [Admin API]
---------------------

 +------------------------+--------+--------------+--------------+-----------------+
 | Name                   | Group  | Handler      | Dependencies | Privileges      |
 +------------------------+--------+--------------+--------------+-----------------+
 | shopware-entity-search | entity | Sw\\Search   |              | <entity>:read   |
 | shopware-entity-upsert | entity | Sw\\Upsert   |              | <entity>:create |
 +------------------------+--------+--------------+--------------+-----------------+

Prompts (1) [Admin API]
-----------------------

 +------------------+--------+-------------+------+-------+
 | Name             | Title  | Handler     | Args | Other |
 +------------------+--------+-------------+------+-------+
 | shopware-context | Ctx    | Sw\\Prompt  |      |       |
 +------------------+--------+-------------+------+-------+

Store API (/store-api/_mcp)
===========================

Tools (3) [Store API]
---------------------

 +----------------------------+-----------+-------------+--------------+------------+
 | Name                       | Group     | Handler     | Dependencies | Privileges |
 +----------------------------+-----------+-------------+--------------+------------+
 | shopware-store-api-context | store-api | Sw\\Context |              |            |
 | create_cart                | discovery | Ucp\\Cart   |              |            |
 | get_order                  | discovery | Ucp\\Order  |              |            |
 +----------------------------+-----------+-------------+--------------+------------+

 Run debug:mcp --scope=store-api to inspect a single MCP server.
"""


def test_it_parses_the_real_table_and_ignores_the_rules_and_header() -> None:
    tools = rc.parse_tools(REAL_OUTPUT)[rc.ADMIN]

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
    assert rc.problems(rc.parse_tools(REAL_OUTPUT)[rc.ADMIN]) == []


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


# ---------------------------------------------------------------------------
# Both servers (shopware/shopware#18848)
# ---------------------------------------------------------------------------
def test_each_server_gets_its_own_registry() -> None:
    registries = rc.parse_tools(BOTH_SCOPES)

    assert set(registries[rc.ADMIN]) == {"shopware-entity-search", "shopware-entity-upsert"}
    assert set(registries[rc.STORE]) == {"shopware-store-api-context", "create_cart", "get_order"}


def test_ucp_names_with_underscores_are_read() -> None:
    """They were dropped before: the name pattern only allowed hyphens."""
    assert "create_cart" in rc.parse_tools(BOTH_SCOPES)[rc.STORE]


def test_rows_of_other_sections_are_never_tools() -> None:
    parsed = rc.parse_tools(BOTH_SCOPES)

    assert all("shopware-context" not in tools for tools in parsed.values())


def test_the_store_api_context_tool_is_not_filed_under_admin() -> None:
    assert "shopware-store-api-context" not in rc.parse_tools(BOTH_SCOPES)[rc.ADMIN]


def test_the_allowlist_filtered_heading_is_recognised() -> None:
    text = "Tools (1/30 allowed) [Admin API]\n| shopware-entity-read | entity | H | | <entity>:read |\n"

    assert rc.parse_tools(text) == {rc.ADMIN: {"shopware-entity-read": "<entity>:read"}}


def test_main_checks_both_servers(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "debug.txt"
    _ = path.write_text(BOTH_SCOPES)

    assert rc.main(["--from-file", str(path)]) == 0
    assert "2 Admin API, 3 Store API" in capsys.readouterr().out


def test_a_store_problem_is_named_by_server(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "debug.txt"
    _ = path.write_text(BOTH_SCOPES.replace("get_order  ", "brand_new  "))

    assert rc.main(["--from-file", str(path)]) == 1
    assert "[Store API] brand_new is registered but unclassified" in capsys.readouterr().out


def test_older_output_without_a_store_block_is_noted_not_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "debug.txt"
    _ = path.write_text(REAL_OUTPUT)

    assert rc.main(["--from-file", str(path)]) == 0
    assert "no Store API section" in capsys.readouterr().out


def test_no_admin_tools_fails(tmp_path: Path) -> None:
    path = tmp_path / "debug.txt"
    _ = path.write_text("Command 'debug:mcp' is not defined.")

    assert rc.main(["--from-file", str(path)]) == 1
