Taken from [Kubernetes OWNERS guide](https://www.kubernetes.dev/docs/guide/owners/#owners)

# OWNERS files
Each directory that contains a unit of independent code or content may also contain an OWNERS file. This file applies to everything within the directory, including the OWNERS file itself, sibling files, and child directories.

OWNERS files are in YAML format and support the following keys:

- approvers: a list of GitHub usernames or aliases that can /approve a PR.
- reviewers: a list of GitHub usernames or aliases that are good candidates to /lgtm a PR.
- root-approvers: optional boolean, honored only in a subdirectory OWNERS file.
- emeritus_approvers a list of GitHub usernames of folks who were previously in the approvers section, but are no longer actively approving code.

## How OWNERS files are resolved

Approval is enforced by the [github-webhook-server](https://myk-org.github.io/github-webhook-server/set-up-owners-and-reviewers.html)
bot, not by GitHub CODEOWNERS.

- Every OWNERS file at a changed file's directory, or at any ancestor of it, matches the PR. All
  matched files apply together; a subdirectory file does not replace its ancestors' lists.
- Each matched file that lists `approvers` requires an approval from one of them.
- Root approvers are required by default. A subdirectory file that sets `root-approvers: False`
  opts out, and that file's own approvers are required instead. Root approvers may still approve,
  and an approval from any root approver satisfies the check for every matched file.
- A matched file that lists `reviewers` but no `approvers` adds no requirement of its own. Root
  approval still applies unless that file sets `root-approvers: False`.
- Do not mix directories that set `root-approvers: False` with directories that do not in one PR:
  the first matched file decides whether root approval is required.

`root-approvers` has no effect in the root `OWNERS` file.
