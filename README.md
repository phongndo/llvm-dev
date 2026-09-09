# llvm-dev

LLVM workspace for Apple Silicon, Linux, and WSL2. Requires Nix with flakes enabled.

```sh
git clone https://github.com/phongndo/llvm-dev.git
cd llvm-dev
git clone https://github.com/phongndo/llvm-project.git
nix develop
just doctor
just configure
just build opt
just test llvm/test/Transforms/InstSimplify
just compdb
```

`just` lists commands. The first test builds suite prerequisites and can be large.
