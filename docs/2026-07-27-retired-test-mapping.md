# Retired runtime test mapping

This mapping records tests whose positive expectation conflicts with the
project-only contract. They are retained until their replacement assertions are
implemented; none are skipped or deleted.

| Existing test | Retired positive assertion | Replacement invariant | Replacement coverage |
| --- | --- | --- | --- |
| `tests/test_server.py::TestRecallScopeParameter::test_recall_tool_has_scope_in_input_schema` | MCP Recall accepts a `scope` selector for cross-Project behavior. | Normal Recall has no scope selector and is pinned to the connected Project. | Assert the FastMCP schema lacks `scope` and `projects`, and rejects a non-connected singular Project before store access. |
| `tests/test_registry.py::TestListFamiliesTool::{test_empty_families_returns_empty_list,test_registered_member_resolves_ulid,test_unregistered_member_excluded_from_project_ids,test_families_sorted_by_name,test_multiple_registered_members}` | MCP exposes legacy Family metadata. | MCP exposes named Pool metadata only, consisting of stable member ULIDs and registry-only member status. | Assert `list_families` is absent; assert `list_pools` reads no database and reports registered/unregistered membership. |
| `tests/test_registry.py::TestFamilyCLIArgparse::{test_family_add_via_main,test_family_add_multiple_paths,test_family_ls_via_main,test_family_rm_whole_family_via_main,test_family_rm_specific_member_via_main}` | The `family` CLI mutates and lists legacy Family membership. | Named Pool CLI owns explicit ULID membership; retired Family commands fail clearly. | Assert `braincell family` is rejected and create/add/decouple Pool operations preserve Project ULIDs and databases. |

The existing tests above will be rewritten only after the corresponding stronger
project-only assertions are present in the test suite.
