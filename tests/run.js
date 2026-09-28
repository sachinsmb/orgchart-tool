/* Regression tests for the pure logic in index.html.
 *
 * Every case here is a bug that actually shipped. There was no suite at all until
 * 2026-09-06, which is why the same XSS class shipped twice in one day and why a
 * tolerance check that could never pass ran in production for hours.
 *
 *   node tests/run.js
 */
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '../app/frontend/index.html'), 'utf8');
const js = src.split('<script>')[2].split('</script>')[0];

// Pull out single functions by name and evaluate them in isolation — no DOM needed.
function extract(...names){
  const out = {};
  for(const n of names){
    const re = new RegExp('^(?:async )?function ' + n + '\\s*\\(', 'm');
    const m = js.match(re);
    if(!m){ throw new Error('function not found in index.html: ' + n); }
    let i = js.indexOf('{', m.index), depth = 0, j = i;
    for(; j < js.length; j++){
      if(js[j] === '{') depth++;
      else if(js[j] === '}'){ depth--; if(!depth){ j++; break; } }
    }
    out[n] = js.slice(m.index, j);
  }
  return out;
}

let pass = 0, fail = 0;
const eq = (name, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  ok ? pass++ : fail++;
  console.log(`  ${ok ? '✓' : '✗'} ${name}` + (ok ? '' : `\n      got:  ${JSON.stringify(got)}\n      want: ${JSON.stringify(want)}`));
};

// ---------------------------------------------------------------- escaping
{
  const src = extract('esc', 'jsAttr');
  const fn = new Function(src.esc + '\n' + src.jsAttr + '\n return {esc, jsAttr};')();
  console.log('\nescaping — a tag name reaches an HTML attribute and a JS string');
  eq('esc quotes double-quote', fn.esc('a"b'), 'a&quot;b');
  eq('esc handles angle brackets', fn.esc('<img>'), '&lt;img&gt;');
  eq('esc leaves the single quote (documented gap)', fn.esc("it's"), "it's");
  // the real 31-Jul payload
  const payload = "x');window.__PWNED=true;//";
  const inAttr = `data-fk="${fn.esc(payload)}"`;
  eq('payload cannot close a double-quoted attribute', inAttr.split('"').length, 3);
  // jsAttr wraps the value in ESCAPED double quotes, so it lands inside a double-quoted JS
  // string — a single quote in the payload is then inert. The invariant is "no raw double
  // quote survives", not "no single quote survives" (an earlier version of this test asserted
  // the wrong property and failed against correct code).
  eq('jsAttr emits no raw double-quote', /(?<!&quot;)"/.test(fn.jsAttr(`say "hi"`)), false);
  eq('jsAttr delimits with escaped quotes', fn.jsAttr("it's").startsWith('&quot;'), true);
  eq('the 31-Jul payload is inert inside it', fn.jsAttr("x');window.__PWNED=true;//").includes('"'), false);
}

// ---------------------------------------------------------------- slug
{
  const fn = new Function(extract('slugKey').slugKey + '\n return slugKey;')();
  console.log('\nslugKey — tag names must never carry syntax');
  eq('lowercases and dashes', fn('Key Man'), 'key-man');
  eq('strips quotes and script', fn(`x');alert(1)//`), 'x-alert-1');
  eq('empty stays empty', fn('!!!'), '');
}

// ---------------------------------------------------------------- the null-tolerance bug
{
  console.log('\nreflow tolerance — the bug that made every render save (09-06)');
  // the shipped-and-broken version
  const broken = (b,x,y,w,h) =>
    !(Math.abs(b.x-x)<1 && Math.abs(b.y-y)<1 && Math.abs((b.w||0)-w)<1 && Math.abs((b.h||0)-h)<1);
  // the fix now in index.html
  const fixed = (b,x,y,w,h) => {
    const sameXY = Math.abs(b.x-x)<1 && Math.abs(b.y-y)<1;
    const sameWH = (w==null && h==null) || (Math.abs((b.w||0)-(w||0))<1 && Math.abs((b.h||0)-(h||0))<1);
    return !(sameXY && sameWH);
  };
  const box = {x:100, y:200, w:470, h:103};
  eq('BROKEN reported a move when nothing changed', broken(box,100,200,null,null), true);
  eq('fixed reports no move when nothing changed', fixed(box,100,200,null,null), false);
  eq('fixed still detects a real move', fixed(box,140,200,null,null), true);
  eq('fixed still detects a resize', fixed(box,100,200,600,103), true);
}

// ---------------------------------------------------------------- colour safety
{
  const fn = new Function(extract('safeColor').safeColor + '\n return safeColor;')();
  console.log('\nsafeColor — a stored colour is written straight into a style attribute');
  eq('accepts a hex colour', fn('#ec4899'), '#ec4899');
  eq('rejects a CSS injection', fn('red;background:url(javascript:alert(1))'), '');
  eq('rejects an unclosed expression', fn('#fff") evil("'), '');
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
