# Security

Pi Workflows can execute shell commands and Pi agents with the permissions of
the user running it. Review third-party workflows before running them, keep
credentials in the environment rather than YAML, and use a container or sandbox
when a workflow is not fully trusted.

Do not open a public issue for a vulnerability. Use GitHub's private
**Security → Report a vulnerability** flow for this repository.
