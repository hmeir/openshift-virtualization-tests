Taken from [Kubernetes OWNERS guide](https://www.kubernetes.dev/docs/guide/owners/#owners)

# OWNERS files
Each directory that contains a unit of independent code or content may also contain an OWNERS file. This file applies to everything within the directory, including the OWNERS file itself, sibling files, and child directories.

OWNERS files are in YAML format and support the following keys:

- approvers: a list of GitHub usernames or aliases that can /approve a PR.
- reviewers: a list of GitHub usernames or aliases that are good candidates to /lgtm a PR.
- emeritus_approvers a list of GitHub usernames of folks who were previously in the approvers section, but are no longer actively approving code.

In addition to the Kubernetes keys above, this repository supports:

- `root-approvers`: an optional boolean (default `true`). Set it to `False` in a
  subdirectory OWNERS file to opt that subtree out of the root-approver
  requirement. A PR that touches only files governed by opted-out OWNERS files
  can then be approved by component approvers instead of a root approver (one
  approver from each applicable OWNERS file).

The nearest OWNERS file and every OWNERS file in its parent directories apply
to a changed file. Root approval is required when any applicable OWNERS file
does not set `root-approvers: False`, or when a changed file is not covered by
any subdirectory OWNERS file (for example, a top-level file such as
`pyproject.toml`).
