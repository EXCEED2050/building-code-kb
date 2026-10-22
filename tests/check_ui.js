/* 引用定位和表格转义的回归检查；运行 node tests/check_ui.js。 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const page = fs.readFileSync(path.join(__dirname, '../src/building_kb/web/static/index.html'), 'utf8');
const source = page.slice(page.indexOf('const esc ='), page.indexOf('const EXAMPLES'))
  + page.slice(page.indexOf('function mdTable'), page.indexOf('/* 手机上宽表'))
  + page.slice(page.indexOf('function renderMd'), page.indexOf('function card'))
  + page.slice(page.indexOf('function findCard'), page.indexOf('async function copyText'));
const context = vm.createContext({});
vm.runInContext(source, context);
for (const text of ['[GB 51251-2017 第4.2.4条 P26]', '出处：GB 51251-2017 第4.2.4条表4.2.4', 'SH/T 3186-2017 第5.1.4条']) {
  const html = context.renderMd(text);
  assert.equal((html.match(/class="ref"/g) || []).length, 1);
  assert.match(html, /data-cl="(?:4.2.4|5.1.4)"/);
}
assert.doesNotMatch(context.renderMd('<img src=x onerror=alert(1)>'), /<img/);
assert.doesNotMatch(context.mdTable('| 高度 | 面积 |\n| <script> | 1000 |'), /<script>/);
assert.match(context.mdTable('| 高度 | 面积 |\n| 4 | 1000 |'), /<td>1000<\/td>/);
const c = (std, cl) => ({dataset:{std,cl}});
const old = c('GB 50016-2014','5.1.8'), current = c('GB 55037-2022','7.3.1');
const round = {querySelectorAll: () => [current]};
assert.equal(context.findCard('GB 55037','7.3.1',round), current);
assert.equal(context.findCard('GB 50016','5.1.8',round), null);
assert.equal(context.findCard('GB 55037-2025','7.3.1',round), null);
assert.equal(context.findCard('GB 50016','5.1.8',{querySelectorAll: () => [old,c('GB 50016-2025','5.1.8')]}), null);
console.log('PASS：方括号与普通引用、逐轮定位、版本歧义、表格及输出转义');
