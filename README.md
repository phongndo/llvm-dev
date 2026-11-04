# llvm-dev

My LLVM workspace on macOS and Linux. Needs Nix with flakes.

```sh
git clone https://github.com/phongndo/llvm-dev.git && cd llvm-dev
git clone https://github.com/phongndo/llvm-project.git
git -C llvm-project remote add upstream https://github.com/llvm/llvm-project.git
nix develop
just sync
just install-t3
t3 project add llvm-project
```

`just sync` moves `llvm-project` to upstream's latest `main` and builds it. `just install-t3`
is once per machine: it lets every T3 worktree of `llvm-project` find this repo by searching
parent folders, so T3's diffs show LLVM changes and nothing is written into a worktree.

In T3, turn off "start from origin" for this project and start threads from `main`. They then
share the main checkout's commit, so its build is their "before" and their builds hit ccache.

`just` lists the rest.
