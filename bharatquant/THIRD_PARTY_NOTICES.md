# Third-party dependency inventory

Inventory checked 2026-11-21 for this local personal application. Python
versions and artifact hashes are recorded in [uv.lock](./uv.lock). Node
versions and integrity hashes are recorded in
[web/package-lock.json](./web/package-lock.json). Python license identifiers
come from installed distribution metadata; where metadata omitted an SPDX
identifier, the bundled license file was inspected.

This is an inventory, not legal advice or authorization to redistribute.
BharatQuant has no declared redistribution license. Before distributing an
application bundle, include the full license texts and copyright notices
required by each dependency and review the complete transitive dependency set.

## Python packages

All packages below were resolved from `https://pypi.org/simple`. Transitive
dependencies are included because they are part of the lock.

| Role | Package | Version | License |
| --- | --- | --- | --- |
| Transitive | [annotated-doc](https://pypi.org/project/annotated-doc/0.0.7/) | 0.0.7 | MIT |
| Transitive | [annotated-types](https://pypi.org/project/annotated-types/0.8.0/) | 0.8.0 | MIT |
| Transitive | [anyio](https://pypi.org/project/anyio/4.16.0/) | 4.16.0 | MIT |
| Runtime | [argon2-cffi](https://pypi.org/project/argon2-cffi/25.1.0/) | 25.1.0 | MIT |
| Transitive | [argon2-cffi-bindings](https://pypi.org/project/argon2-cffi-bindings/26.2.0/) | 26.2.0 | MIT |
| Transitive | [ast-serialize](https://pypi.org/project/ast-serialize/0.13.1/) | 0.13.1 | MIT |
| Transitive | [attrs](https://pypi.org/project/attrs/26.1.0/) | 26.1.0 | MIT |
| Transitive | [certifi](https://pypi.org/project/certifi/2026.11.20/) | 2026.11.20 | MPL-2.0 |
| Transitive | [cffi](https://pypi.org/project/cffi/2.1.1/) | 2.1.1 | MIT-0 |
| Transitive | [click](https://pypi.org/project/click/8.5.1/) | 8.5.1 | BSD-3-Clause |
| Transitive | [colorama](https://pypi.org/project/colorama/0.4.6/) | 0.4.6 | BSD-3-Clause (bundled license) |
| Runtime | [fastapi](https://pypi.org/project/fastapi/0.165.0/) | 0.165.0 | MIT |
| Transitive | [h11](https://pypi.org/project/h11/0.16.0/) | 0.16.0 | MIT |
| Build and development | [hatchling](https://pypi.org/project/hatchling/1.33.0/) | 1.33.0 | MIT |
| Transitive | [httpcore](https://pypi.org/project/httpcore/1.0.9/) | 1.0.9 | BSD-3-Clause |
| Development | [httpx](https://pypi.org/project/httpx/0.28.1/) | 0.28.1 | BSD-3-Clause |
| Transitive | [idna](https://pypi.org/project/idna/3.20/) | 3.20 | BSD-3-Clause |
| Transitive | [iniconfig](https://pypi.org/project/iniconfig/2.3.1/) | 2.3.1 | MIT |
| Runtime | [jsonschema](https://pypi.org/project/jsonschema/4.26.0/) | 4.26.0 | MIT |
| Transitive | [jsonschema-specifications](https://pypi.org/project/jsonschema-specifications/2025.9.1/) | 2025.9.1 | MIT |
| Transitive | [librt](https://pypi.org/project/librt/0.17.0/) | 0.17.0 | MIT |
| Development | [mypy](https://pypi.org/project/mypy/2.4.1/) | 2.4.1 | MIT |
| Transitive | [mypy-extensions](https://pypi.org/project/mypy-extensions/1.2.0/) | 1.2.0 | MIT |
| Transitive | [opentelemetry-api](https://pypi.org/project/opentelemetry-api/1.45.2/) | 1.45.2 | Apache-2.0 |
| Transitive | [packaging](https://pypi.org/project/packaging/26.3/) | 26.3 | Apache-2.0 OR BSD-2-Clause |
| Transitive | [pathspec](https://pypi.org/project/pathspec/1.1.1/) | 1.1.1 | MPL-2.0 (bundled license) |
| Transitive | [pluggy](https://pypi.org/project/pluggy/1.7.0/) | 1.7.0 | MIT |
| Transitive | [pydantic](https://pypi.org/project/pydantic/2.14.4/) | 2.14.4 | MIT |
| Transitive | [pydantic-core](https://pypi.org/project/pydantic-core/2.50.5/) | 2.50.5 | MIT |
| Transitive | [pycparser](https://pypi.org/project/pycparser/3.12/) | 3.12 | BSD-3-Clause |
| Transitive | [Pygments](https://pypi.org/project/pygments/2.21.0/) | 2.21.0 | BSD-2-Clause |
| Development | [pytest](https://pypi.org/project/pytest/9.2.0/) | 9.2.0 | MIT |
| Runtime | [referencing](https://pypi.org/project/referencing/0.37.0/) | 0.37.0 | MIT |
| Transitive | [rpds-py](https://pypi.org/project/rpds-py/2026.9.1/) | 2026.9.1 | MIT |
| Development | [ruff](https://pypi.org/project/ruff/0.17.6/) | 0.17.6 | MIT |
| Transitive | [starlette](https://pypi.org/project/starlette/1.10.0/) | 1.10.0 | BSD-3-Clause |
| Transitive | [tomlkit](https://pypi.org/project/tomlkit/0.15.2/) | 0.15.2 | MIT |
| Transitive | [trove-classifiers](https://pypi.org/project/trove-classifiers/2026.9.21.13/) | 2026.9.21.13 | Apache-2.0 (bundled license) |
| Development | [types-jsonschema](https://pypi.org/project/types-jsonschema/4.26.0.20261006/) | 4.26.0.20261006 | Apache-2.0 |
| Transitive | [typing-extensions](https://pypi.org/project/typing-extensions/4.16.0/) | 4.16.0 | PSF-2.0 |
| Transitive | [typing-inspection](https://pypi.org/project/typing-inspection/0.4.5/) | 0.4.5 | MIT |
| Runtime | [tzdata](https://pypi.org/project/tzdata/2026.6/) | 2026.6 | Apache-2.0 |
| Runtime | [uvicorn](https://pypi.org/project/uvicorn/0.54.0/) | 0.54.0 | BSD-3-Clause |

## Node packages

All packages below resolve from `https://registry.npmjs.org`; their resolved
URLs and integrity values are in the lockfile. All 77 lock entries are listed,
including optional platform-specific binaries not installed on Windows.

| Role | Package | Version | Platforms | License |
| --- | --- | --- | --- | --- |
| Optional transitive | [@emnapi/core](https://www.npmjs.com/package/@emnapi/core/v/2.0.0-beta.2) | 2.0.0-beta.2 | all | MIT |
| Optional transitive | [@emnapi/runtime](https://www.npmjs.com/package/@emnapi/runtime/v/2.0.0-beta.2) | 2.0.0-beta.2 | all | MIT |
| Optional transitive | [@emnapi/wasi-threads](https://www.npmjs.com/package/@emnapi/wasi-threads/v/2.2.0) | 2.2.0 | all | MIT |
| Optional transitive | [@napi-rs/wasm-runtime](https://www.npmjs.com/package/@napi-rs/wasm-runtime/v/1.2.6) | 1.2.6 | all | MIT |
| Transitive | [@oxc-project/types](https://www.npmjs.com/package/@oxc-project/types/v/0.156.0) | 0.156.0 | all | MIT |
| Optional transitive | [@rolldown/binding-android-arm-eabi](https://www.npmjs.com/package/@rolldown/binding-android-arm-eabi/v/1.2.19) | 1.2.19 | android, arm | MIT |
| Optional transitive | [@rolldown/binding-android-arm64](https://www.npmjs.com/package/@rolldown/binding-android-arm64/v/1.2.19) | 1.2.19 | android, arm64 | MIT |
| Optional transitive | [@rolldown/binding-darwin-arm64](https://www.npmjs.com/package/@rolldown/binding-darwin-arm64/v/1.2.19) | 1.2.19 | darwin, arm64 | MIT |
| Optional transitive | [@rolldown/binding-darwin-x64](https://www.npmjs.com/package/@rolldown/binding-darwin-x64/v/1.2.19) | 1.2.19 | darwin, x64 | MIT |
| Optional transitive | [@rolldown/binding-freebsd-x64](https://www.npmjs.com/package/@rolldown/binding-freebsd-x64/v/1.2.19) | 1.2.19 | freebsd, x64 | MIT |
| Optional transitive | [@rolldown/binding-linux-arm-gnueabihf](https://www.npmjs.com/package/@rolldown/binding-linux-arm-gnueabihf/v/1.2.19) | 1.2.19 | linux, arm | MIT |
| Optional transitive | [@rolldown/binding-linux-arm64-gnu](https://www.npmjs.com/package/@rolldown/binding-linux-arm64-gnu/v/1.2.19) | 1.2.19 | linux, arm64 | MIT |
| Optional transitive | [@rolldown/binding-linux-arm64-musl](https://www.npmjs.com/package/@rolldown/binding-linux-arm64-musl/v/1.2.19) | 1.2.19 | linux, arm64 | MIT |
| Optional transitive | [@rolldown/binding-linux-ppc64-gnu](https://www.npmjs.com/package/@rolldown/binding-linux-ppc64-gnu/v/1.2.19) | 1.2.19 | linux, ppc64 | MIT |
| Optional transitive | [@rolldown/binding-linux-s390x-gnu](https://www.npmjs.com/package/@rolldown/binding-linux-s390x-gnu/v/1.2.19) | 1.2.19 | linux, s390x | MIT |
| Optional transitive | [@rolldown/binding-linux-x64-gnu](https://www.npmjs.com/package/@rolldown/binding-linux-x64-gnu/v/1.2.19) | 1.2.19 | linux, x64 | MIT |
| Optional transitive | [@rolldown/binding-linux-x64-musl](https://www.npmjs.com/package/@rolldown/binding-linux-x64-musl/v/1.2.19) | 1.2.19 | linux, x64 | MIT |
| Optional transitive | [@rolldown/binding-openharmony-arm64](https://www.npmjs.com/package/@rolldown/binding-openharmony-arm64/v/1.2.19) | 1.2.19 | openharmony, arm64 | MIT |
| Optional transitive | [@rolldown/binding-wasm32-wasi](https://www.npmjs.com/package/@rolldown/binding-wasm32-wasi/v/1.2.19) | 1.2.19 | wasm32 | MIT |
| Optional transitive | [@rolldown/binding-win32-arm64-msvc](https://www.npmjs.com/package/@rolldown/binding-win32-arm64-msvc/v/1.2.19) | 1.2.19 | win32, arm64 | MIT |
| Optional transitive | [@rolldown/binding-win32-x64-msvc](https://www.npmjs.com/package/@rolldown/binding-win32-x64-msvc/v/1.2.19) | 1.2.19 | win32, x64 | MIT |
| Transitive | [@rolldown/pluginutils](https://www.npmjs.com/package/@rolldown/pluginutils/v/1.1.1) | 1.1.1 | all | MIT |
| Optional transitive | [@tybys/wasm-util](https://www.npmjs.com/package/@tybys/wasm-util/v/0.10.5) | 0.10.5 | all | MIT |
| Development | [@types/node](https://www.npmjs.com/package/@types/node/v/26.6.6) | 26.6.6 | all | MIT |
| Development | [@types/react](https://www.npmjs.com/package/@types/react/v/19.3.0) | 19.3.0 | all | MIT |
| Development | [@types/react-dom](https://www.npmjs.com/package/@types/react-dom/v/19.3.0) | 19.3.0 | all | MIT |
| Optional transitive | [@typescript/typescript-aix-ppc64](https://www.npmjs.com/package/@typescript/typescript-aix-ppc64/v/7.0.2) | 7.0.2 | aix, ppc64 | Apache-2.0 |
| Optional transitive | [@typescript/typescript-darwin-arm64](https://www.npmjs.com/package/@typescript/typescript-darwin-arm64/v/7.0.2) | 7.0.2 | darwin, arm64 | Apache-2.0 |
| Optional transitive | [@typescript/typescript-darwin-x64](https://www.npmjs.com/package/@typescript/typescript-darwin-x64/v/7.0.2) | 7.0.2 | darwin, x64 | Apache-2.0 |
| Optional transitive | [@typescript/typescript-freebsd-arm64](https://www.npmjs.com/package/@typescript/typescript-freebsd-arm64/v/7.0.2) | 7.0.2 | freebsd, arm64 | Apache-2.0 |
| Optional transitive | [@typescript/typescript-freebsd-x64](https://www.npmjs.com/package/@typescript/typescript-freebsd-x64/v/7.0.2) | 7.0.2 | freebsd, x64 | Apache-2.0 |
| Optional transitive | [@typescript/typescript-linux-arm](https://www.npmjs.com/package/@typescript/typescript-linux-arm/v/7.0.2) | 7.0.2 | linux, arm | Apache-2.0 |
| Optional transitive | [@typescript/typescript-linux-arm64](https://www.npmjs.com/package/@typescript/typescript-linux-arm64/v/7.0.2) | 7.0.2 | linux, arm64 | Apache-2.0 |
| Optional transitive | [@typescript/typescript-linux-loong64](https://www.npmjs.com/package/@typescript/typescript-linux-loong64/v/7.0.2) | 7.0.2 | linux, loong64 | Apache-2.0 |
| Optional transitive | [@typescript/typescript-linux-mips64el](https://www.npmjs.com/package/@typescript/typescript-linux-mips64el/v/7.0.2) | 7.0.2 | linux, mips64el | Apache-2.0 |
| Optional transitive | [@typescript/typescript-linux-ppc64](https://www.npmjs.com/package/@typescript/typescript-linux-ppc64/v/7.0.2) | 7.0.2 | linux, ppc64 | Apache-2.0 |
| Optional transitive | [@typescript/typescript-linux-riscv64](https://www.npmjs.com/package/@typescript/typescript-linux-riscv64/v/7.0.2) | 7.0.2 | linux, riscv64 | Apache-2.0 |
| Optional transitive | [@typescript/typescript-linux-s390x](https://www.npmjs.com/package/@typescript/typescript-linux-s390x/v/7.0.2) | 7.0.2 | linux, s390x | Apache-2.0 |
| Optional transitive | [@typescript/typescript-linux-x64](https://www.npmjs.com/package/@typescript/typescript-linux-x64/v/7.0.2) | 7.0.2 | linux, x64 | Apache-2.0 |
| Optional transitive | [@typescript/typescript-netbsd-arm64](https://www.npmjs.com/package/@typescript/typescript-netbsd-arm64/v/7.0.2) | 7.0.2 | netbsd, arm64 | Apache-2.0 |
| Optional transitive | [@typescript/typescript-netbsd-x64](https://www.npmjs.com/package/@typescript/typescript-netbsd-x64/v/7.0.2) | 7.0.2 | netbsd, x64 | Apache-2.0 |
| Optional transitive | [@typescript/typescript-openbsd-arm64](https://www.npmjs.com/package/@typescript/typescript-openbsd-arm64/v/7.0.2) | 7.0.2 | openbsd, arm64 | Apache-2.0 |
| Optional transitive | [@typescript/typescript-openbsd-x64](https://www.npmjs.com/package/@typescript/typescript-openbsd-x64/v/7.0.2) | 7.0.2 | openbsd, x64 | Apache-2.0 |
| Optional transitive | [@typescript/typescript-sunos-x64](https://www.npmjs.com/package/@typescript/typescript-sunos-x64/v/7.0.2) | 7.0.2 | sunos, x64 | Apache-2.0 |
| Optional transitive | [@typescript/typescript-win32-arm64](https://www.npmjs.com/package/@typescript/typescript-win32-arm64/v/7.0.2) | 7.0.2 | win32, arm64 | Apache-2.0 |
| Optional transitive | [@typescript/typescript-win32-x64](https://www.npmjs.com/package/@typescript/typescript-win32-x64/v/7.0.2) | 7.0.2 | win32, x64 | Apache-2.0 |
| Development | [@vitejs/plugin-react](https://www.npmjs.com/package/@vitejs/plugin-react/v/6.3.0) | 6.3.0 | all | MIT |
| Transitive | [csstype](https://www.npmjs.com/package/csstype/v/3.2.4) | 3.2.4 | all | MIT |
| Transitive | [detect-libc](https://www.npmjs.com/package/detect-libc/v/2.1.2) | 2.1.2 | all | Apache-2.0 |
| Transitive | [fdir](https://www.npmjs.com/package/fdir/v/6.5.0) | 6.5.0 | all | MIT |
| Optional transitive | [fsevents](https://www.npmjs.com/package/fsevents/v/2.3.3) | 2.3.3 | darwin | MIT |
| Transitive | [lightningcss](https://www.npmjs.com/package/lightningcss/v/1.33.0) | 1.33.0 | all | MPL-2.0 |
| Optional transitive | [lightningcss-android-arm64](https://www.npmjs.com/package/lightningcss-android-arm64/v/1.33.0) | 1.33.0 | android, arm64 | MPL-2.0 |
| Optional transitive | [lightningcss-darwin-arm64](https://www.npmjs.com/package/lightningcss-darwin-arm64/v/1.33.0) | 1.33.0 | darwin, arm64 | MPL-2.0 |
| Optional transitive | [lightningcss-darwin-x64](https://www.npmjs.com/package/lightningcss-darwin-x64/v/1.33.0) | 1.33.0 | darwin, x64 | MPL-2.0 |
| Optional transitive | [lightningcss-freebsd-x64](https://www.npmjs.com/package/lightningcss-freebsd-x64/v/1.33.0) | 1.33.0 | freebsd, x64 | MPL-2.0 |
| Optional transitive | [lightningcss-linux-arm-gnueabihf](https://www.npmjs.com/package/lightningcss-linux-arm-gnueabihf/v/1.33.0) | 1.33.0 | linux, arm | MPL-2.0 |
| Optional transitive | [lightningcss-linux-arm64-gnu](https://www.npmjs.com/package/lightningcss-linux-arm64-gnu/v/1.33.0) | 1.33.0 | linux, arm64 | MPL-2.0 |
| Optional transitive | [lightningcss-linux-arm64-musl](https://www.npmjs.com/package/lightningcss-linux-arm64-musl/v/1.33.0) | 1.33.0 | linux, arm64 | MPL-2.0 |
| Optional transitive | [lightningcss-linux-x64-gnu](https://www.npmjs.com/package/lightningcss-linux-x64-gnu/v/1.33.0) | 1.33.0 | linux, x64 | MPL-2.0 |
| Optional transitive | [lightningcss-linux-x64-musl](https://www.npmjs.com/package/lightningcss-linux-x64-musl/v/1.33.0) | 1.33.0 | linux, x64 | MPL-2.0 |
| Optional transitive | [lightningcss-win32-arm64-msvc](https://www.npmjs.com/package/lightningcss-win32-arm64-msvc/v/1.33.0) | 1.33.0 | win32, arm64 | MPL-2.0 |
| Optional transitive | [lightningcss-win32-x64-msvc](https://www.npmjs.com/package/lightningcss-win32-x64-msvc/v/1.33.0) | 1.33.0 | win32, x64 | MPL-2.0 |
| Transitive | [nanoid](https://www.npmjs.com/package/nanoid/v/3.3.20) | 3.3.20 | all | MIT |
| Transitive | [picocolors](https://www.npmjs.com/package/picocolors/v/1.1.1) | 1.1.1 | all | ISC |
| Transitive | [picomatch](https://www.npmjs.com/package/picomatch/v/4.0.7) | 4.0.7 | all | MIT |
| Transitive | [postcss](https://www.npmjs.com/package/postcss/v/8.5.33) | 8.5.33 | all | MIT |
| Runtime | [react](https://www.npmjs.com/package/react/v/19.3.1) | 19.3.1 | all | MIT |
| Runtime | [react-dom](https://www.npmjs.com/package/react-dom/v/19.3.1) | 19.3.1 | all | MIT |
| Transitive | [rolldown](https://www.npmjs.com/package/rolldown/v/1.2.19) | 1.2.19 | all | MIT |
| Transitive | [scheduler](https://www.npmjs.com/package/scheduler/v/0.28.0) | 0.28.0 | all | MIT |
| Transitive | [source-map-js](https://www.npmjs.com/package/source-map-js/v/1.2.4) | 1.2.4 | all | BSD-3-Clause |
| Transitive | [tinyglobby](https://www.npmjs.com/package/tinyglobby/v/0.2.17) | 0.2.17 | all | MIT |
| Optional transitive | [tslib](https://www.npmjs.com/package/tslib/v/2.9.1) | 2.9.1 | all | 0BSD |
| Development | [typescript](https://www.npmjs.com/package/typescript/v/7.0.2) | 7.0.2 | all | Apache-2.0 |
| Transitive | [undici-types](https://www.npmjs.com/package/undici-types/v/8.9.0) | 8.9.0 | all | MIT |
| Development | [vite](https://www.npmjs.com/package/vite/v/8.3.8) | 8.3.8 | all | MIT |

## CI actions

These are build-time workflow dependencies, pinned to release commits:

| Action | Release | Commit | License |
| --- | --- | --- | --- |
| [actions/checkout](https://github.com/actions/checkout/tree/3d3c42e5aac5ba805825da76410c181273ba90b1) | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` | MIT |
| [actions/setup-python](https://github.com/actions/setup-python/tree/6d4fd22b20465ccc0f7354476588e8df7c9ca0e0) | v7.1.0 | `6d4fd22b20465ccc0f7354476588e8df7c9ca0e0` | MIT |
| [actions/setup-node](https://github.com/actions/setup-node/tree/949feb2413d6458794dcd2491c4babbbce0c15c1) | v7.1.0 | `949feb2413d6458794dcd2491c4babbbce0c15c1` | MIT |
| [astral-sh/setup-uv](https://github.com/astral-sh/setup-uv/tree/1db35dd8eee11c5894872e0b383ae6c406cf738d) | v10.4.0 | `1db35dd8eee11c5894872e0b383ae6c406cf738d` | MIT |

## License references

- [Apache-2.0](https://spdx.org/licenses/Apache-2.0.html)
- [BSD-2-Clause](https://spdx.org/licenses/BSD-2-Clause.html)
- [BSD-3-Clause](https://spdx.org/licenses/BSD-3-Clause.html)
- [0BSD](https://spdx.org/licenses/0BSD.html)
- [ISC](https://spdx.org/licenses/ISC.html)
- [MIT](https://spdx.org/licenses/MIT.html)
- [MIT-0](https://spdx.org/licenses/MIT-0.html)
- [MPL-2.0](https://spdx.org/licenses/MPL-2.0.html)
- [PSF-2.0](https://spdx.org/licenses/PSF-2.0.html)
