// Validate canonical company records, frontend counts, and product index fields.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const root = __dirname;
const loaded = {};
const context = {window: {_loadCompany: (name, apps) => { loaded[name] = apps; }}};
vm.createContext(context);
for (const file of fs.readdirSync(path.join(root, 'data')).filter(f => f.endsWith('.js'))) {
  vm.runInContext(fs.readFileSync(path.join(root, 'data', file), 'utf8'), context, {filename: file});
}
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const start = html.indexOf('const companiesData = ');
const end = html.indexOf('\n];', start) + 3;
if (start < 0 || end < 3) throw new Error('Company metadata missing');
const metadata = {};
vm.createContext(metadata);
vm.runInContext(html.slice(start, end).replace(/^const companiesData\s*=\s*/, 'globalThis.companies = '), metadata);
const apps = Object.values(loaded).flat();
const key = app => `${app.platform}:${app.pkg_or_id}`;
const keys = apps.map(key);
const mismatches = metadata.companies.filter(company => {
  const records = loaded[company.name] || [];
  return records.length !== company.total_count ||
    records.filter(a => a.platform === 'GP').length !== company.gp_count ||
    records.filter(a => a.platform === 'iOS').length !== company.ios_count;
}).map(company => company.name);
for (const name of Object.keys(loaded)) {
  if (!metadata.companies.some(c => c.name === name)) mismatches.push(name);
}
let index;
const indexContext = {window: {_loadProductIndex: records => { index = records; }}};
vm.createContext(indexContext);
vm.runInContext(fs.readFileSync(path.join(root, 'product_index.js'), 'utf8'), indexContext);
if (!Array.isArray(index)) throw new Error('Product index missing');
const byKey = new Map(apps.map(app => [key(app), app]));
const indexMismatches = index.filter(record => {
  const app = byKey.get(key(record));
  return !app || Object.keys(record).some(field => JSON.stringify(record[field]) !== JSON.stringify(app[field]));
}).map(key);
const result = {
  companies: Object.keys(loaded).length, products: apps.length,
  duplicates: keys.length - new Set(keys).size, companyMismatches: mismatches,
  indexProducts: index.length, indexDuplicates: index.length - new Set(index.map(key)).size,
  indexMismatches, missingLiveDates: apps.filter(a => !a.removed && !a.release_date).map(key)
};
console.log(JSON.stringify(result, null, 2));
if (result.duplicates || mismatches.length || result.indexDuplicates || indexMismatches.length ||
    index.length !== apps.length || result.missingLiveDates.length) process.exitCode = 1;
