# Decision records

This directory is the repository's decision-record layer. It is intentionally
minimal: one file per decision, four required sections, no tooling.

## Why this exists

The rationale for this repository always existed — but it lived in PR bodies and
commit messages, which is to say it lived somewhere no reader arrives and no tool
can reach. Decision candidates reported by the repowise index cannot be accepted
without an in-tree record: accepting one means putting the choice, its cost, and
its rejected alternative on disk where the next reader will actually find them.
A repository that has made choices but recorded none of them has no memory, only
artefacts.

## Convention

One file per decision, named `NNNN-short-slug.md`. Numbering follows creation
order. A number is **never** reused and **never** reassigned: once `0042` exists,
`0042` is permanently that decision, even if the decision is later superseded or
rejected. Renumbering would break every link that points at a record, and those
links are the only thing that makes this directory a record rather than a folder.
Superseding is done by editing the `Status` line of the old record and writing a
new one — never by rewriting the old one in place.

Start from [0000-template.md](0000-template.md).

## Status vocabulary

| Status                 | Meaning                                                                     |
| ---------------------- | --------------------------------------------------------------------------- |
| `proposed`             | Written down, not yet agreed. The default for anything an agent produces.   |
| `accepted`             | Agreed and in force.                                                        |
| `superseded by NNNN`   | Replaced by the record numbered `NNNN`, which carries the current choice.   |
| `rejected`             | Considered and turned down. Kept, because "we did this and declined" is a decision too. |

**Only a human moves a record to `accepted`.** An agent always writes `proposed`,
whatever the strength of its evidence. This is the one rule in this directory
that no automation may relax: a machine cannot tell a settled choice from a
plausible one.

## How this reaches repowise

A human copies the record's `Title` and `Rationale` into the decision form in the
repowise dashboard. The record in this directory is the source of truth; the
dashboard is a mirror of it, not the other way round.

Nothing automates this, and that is a fact about repowise rather than an omission
here. The indexer's MCP server is hosted and read-only, so no script in this
repository can write to it. Every decision therefore crosses that boundary by a
person, deliberately — which is also why acceptance stays with a human.