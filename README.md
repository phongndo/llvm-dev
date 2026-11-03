# llvm-dev

My LLVM workspace on macOS and Linux. Needs Nix with flakes.

```sh
git clone https://github.com/phongndo/llvm-dev.git && cd llvm-dev
git clone https://github.com/phongndo/llvm-project.git
git -C llvm-project remote add upstream https://github.com/llvm/llvm-project.git
nix develop
just configure
just build opt
```

`just` lists the rest.
