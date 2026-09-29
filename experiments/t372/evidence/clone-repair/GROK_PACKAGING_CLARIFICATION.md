**ACCEPT** for the narrow T-372 input-packaging repair. The R2 bridge and any live release stay outside this verdict.

The ref-tip blocker is withdrawn. Protocol v2 `parse_want` admits a want when the object exists. `gitprotocol-v2` states wants are not limited to advertised objects. The v0/v1 `receive_needs` tip gate and `uploadpack.allowAnySHA1InWant` / `allowReachableSHA1InWant` do not apply to this verified v2 path. Git 2.54 defaults to v2.

The counterprobe matches the code: source `show-ref` exits 1, both allow-SHA keys are unset, `git fetch --depth=1` of `580a9c2d…` exits 0, and the trace shows `version 2` plus an accepted want. The regression passed on macOS and on GitHub Ubuntu CI for Python 3.11, 3.12, and 3.13 (run 36531944759). The initial repair needs no further production change for this claim.

`uploadpack.packObjectsHook` stays nonblocking and is inapplicable here: git-config honors it only from protected config, which this helper scrubs. The other original comments stay nonblocking. This is not a broader audit.
