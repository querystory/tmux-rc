// Browser artifacts are committed so Python-only installs need no Node runtime.
// CI installs from the lockfile before comparing every generated file byte-for-byte.
import { readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const vendor = path.join(root, 'web/m/vendor');
const check = process.argv.includes('--check');
const lock = JSON.parse(await readFile(path.join(root, 'package-lock.json'), 'utf8'));
const files = new Map();
const versions = {};
const bundles = {
  echarts: ['dist/echarts.min.js', 'echarts.min.js'],
  wordcloud: ['src/wordcloud2.js', 'wordcloud.js'],
  'markdown-it': ['dist/browser/markdown-it.esm.min.mjs', 'markdown-it.mjs'],
};
for (const [name, [bundle, filename]] of Object.entries(bundles)) {
  const dir = path.join(root, 'node_modules', name);
  const pkg = JSON.parse(await readFile(path.join(dir, 'package.json'), 'utf8'));
  const entry = lock.packages[`node_modules/${name}`];
  if (pkg.version !== entry.version) throw new Error(`${name}: run npm ci before generating assets`);
  versions[name] = { version: pkg.version, integrity: entry.integrity };
  files.set(filename, await readFile(path.join(dir, bundle)));
}
for (const name of ['LICENSE', 'NOTICE']) {
  files.set(`echarts.${name}`, await readFile(path.join(root, 'node_modules/echarts', name)));
}
files.set('echarts.LICENSE-d3', await readFile(path.join(root, 'node_modules/echarts/licenses/LICENSE-d3')));
files.set('wordcloud.LICENSE', await readFile(path.join(root, 'node_modules/wordcloud/LICENSE')));
files.set('markdown-it.LICENSE', await readFile(path.join(root, 'node_modules/markdown-it/LICENSE')));
files.set('versions.json', Buffer.from(`${JSON.stringify(versions, null, 2)}\n`));

const unexpected = (await readdir(vendor)).filter(name => name !== 'README.md' && !files.has(name));
const different = [];
for (const [name, bytes] of files) {
  let actual;
  try { actual = await readFile(path.join(vendor, name)); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (!actual?.equals(bytes)) different.push(name);
}
if (check) {
  if (different.length || unexpected.length) {
    console.error(`Vendored assets differ: ${[...different, ...unexpected].join(', ')}. Run npm run vendor:build and commit the result.`);
    process.exitCode = 1;
  } else console.log('Vendored assets match the locked dependencies.');
} else {
  for (const name of unexpected) await rm(path.join(vendor, name));
  for (const [name, bytes] of files) await writeFile(path.join(vendor, name), bytes);
  console.log('Updated browser assets from the locked dependencies.');
}
