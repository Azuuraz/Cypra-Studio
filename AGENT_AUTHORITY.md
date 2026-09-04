# Agent Authority

## Primary editors

**Nexus Prime** and **Cypra** are the only CodeAgentSwarm agents permitted to mutate files or execute shell commands. They can inspect the selected workspace, plan changes, edit files, run diagnostics/tests, verify the filesystem, and delegate review work.

## Delegated specialists

Other agents are read-only reviewers/diagnosticians inside CodeAgentSwarm. They may inspect assigned files and return analysis, but they cannot write, delete, rename files, or execute shell commands.

This policy is enforced by the executor, not by a prompt.

## Workspace boundary

All file operations resolve inside the task workspace and are rejected if path traversal, absolute-path escape, or symlink/junction resolution leaves the CypraWorkShop project root.

## Verification

Agent messages are not treated as proof. CodeAgentSwarm records actual file operations, command results, and verification outcomes.
