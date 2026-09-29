# Browser dependencies

These files are generated from the root `package.json` and `package-lock.json`.
They are committed so the daemon and Python wheels serve a self-contained UI without
Node or CDN access at runtime. `versions.json` records the package versions and npm
tarball integrity hashes. Do not edit the generated JavaScript directly.

After a dependency update (including a Dependabot PR), run from the repository root:

```sh
npm ci --ignore-scripts
npm run vendor:build
npm run vendor:check
```

Commit the manifest/lockfile changes and regenerated files together. CI installs the
locked packages, compares the generated files byte-for-byte, and runs `npm audit`
(failing on moderate or higher advisories). Dependabot monitors npm as well as Python and
GitHub Actions. A manifest-only bump cannot pass CI with an old browser bundle.

The chart loader uses stable filenames, so updating a dependency does not require an
application-code change. Test the atlas, topic clicks, themes, resizing, and history
chart after updates. ECharts renders the history chart; standalone wordcloud2 renders
the topic cloud, so its former ECharts-5-only extension no longer blocks security updates.

Both libraries' licenses and ECharts' NOTICE are copied directly from their npm packages.

Upstream: https://github.com/apache/echarts and https://github.com/timdream/wordcloud2.js
