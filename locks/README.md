# Runtime dependency locks

`environment.yml` is the human-maintained solve input. Production Docker
builds never solve it. They install the architecture-specific explicit lock:

- `conda-linux-64.lock`
- `conda-linux-aarch64.lock`

Both files were generated with `conda-lock==4.0.2` and micromamba, with CUDA
disabled:

```shell
conda-lock lock --micromamba --without-cuda --strip-auth \
  -f environment.yml -p linux-64 -p linux-aarch64 -k explicit \
  --filename-template 'locks/conda-{platform}.lock'
```

Every Conda artifact has an exact URL and package hash. Every Python artifact
has an exact URL and SHA-256, except Hummingbot itself, whose equivalent
immutable identity is the Git commit
`2bfaccc48dd49e71a5b6d9b3011808e127dd00cd`.

CI derives the lock-set identity in a stable filename order:

```shell
python3 scripts/verify_supply_chain.py
```

That digest, the fork source commit, the Hummingbot core commit, and the base
image digest are embedded in the final image labels.
