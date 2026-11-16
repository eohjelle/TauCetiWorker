Exact trusted assets from TauCetiProject/TauCetiTools commit
11e608b37033d5a3e775786b14a8ae97603d949a:
  tauceti_tools/assets/scripts/lint-style.sh
  tauceti_tools/assets/scripts/HeaderStyle.lean

These reproduce the desktop's full-library exec argument overflow. The test
substitutes only source discovery and the Lake executable with local fixtures;
it runs this actual Bash program across the host's exec argument limit.

Upstream fix83befc4721a8d81fea3f98f266d3bc3565ad67b7 changes only the unbounded
header invocation to NUL-separated xargs batches plus explanatory comments.
The adapter's output is byte-identical to that fixed script, also shipped in
Toolse4ed3eab96457b7425b494b0ea1952319f1d12d6. HeaderStyle.lean is identical at
all three commits. The full text-style invocation remains unchanged.

Source: https://github.com/TauCetiProject/TauCetiTools
Verified directly against the local full Git history and deployed pinned assets.
Do not reformat these fixtures: their exact hashes are part of the adapter contract.
