import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
const indexHtml = readFileSync(new URL("../index.html", import.meta.url), "utf8");

function mediaBlocks(query) {
  const blocks = [];
  const pattern = /@media([^{]+)\{/g;
  let match;
  while ((match = pattern.exec(styles)) !== null) {
    if (!match[1].includes(query)) {
      continue;
    }
    let depth = 1;
    let cursor = pattern.lastIndex;
    while (cursor < styles.length && depth > 0) {
      if (styles[cursor] === "{") depth += 1;
      if (styles[cursor] === "}") depth -= 1;
      cursor += 1;
    }
    blocks.push(styles.slice(pattern.lastIndex, cursor - 1));
  }
  return blocks;
}

test("the mobile shell replaces the sidebar strip with a drawer", () => {
  assert.ok(
    !/@media \(max-width: 800px\)/.test(styles),
    "the old 800px strip breakpoint should be gone",
  );
  const blocks = mediaBlocks("max-width: 1024px");
  assert.ok(blocks.length > 0, "a 1024px breakpoint should exist");
  const shell = blocks.join("\n");
  assert.match(shell, /\.sidebar\s*\{[^}]*position:\s*fixed/s);
  assert.match(shell, /\.sidebar--open\s*\{[^}]*transform:\s*translateX\(0\)/s);
  assert.ok(
    !/\.sidebar-nav\s*\{[^}]*overflow-x:\s*auto/s.test(styles),
    "the nav must not be a horizontal scroll strip",
  );
});

test("form fields are 16px on narrow screens so iOS does not zoom", () => {
  const blocks = mediaBlocks("max-width: 639px");
  const block = blocks.join("\n");
  assert.match(block, /input[^{]*,/);
  assert.match(block, /font-size:\s*1rem/);
});

test("the viewport allows safe-area insets without disabling zoom", () => {
  assert.match(indexHtml, /viewport-fit=cover/);
  assert.ok(
    !/maximum-scale/.test(indexHtml),
    "maximum-scale would disable pinch-zoom",
  );
});

test("tables scroll inside their own region, never the page", () => {
  assert.match(styles, /\.table-scroll\s*\{[^}]*overflow-x:\s*auto/s);
  const blocks = mediaBlocks("max-width: 767px");
  const block = blocks.join("\n");
  assert.match(block, /\.table-scroll \.judgment-table\s*\{[^}]*min-width:\s*620px/s);
  assert.match(block, /position:\s*sticky/);
});

test("every 100vh has a 100dvh companion on the same rule", () => {
  const vh = styles.match(/\b100vh\b/g) ?? [];
  const dvh = styles.match(/\b100dvh\b/g) ?? [];
  assert.equal(
    vh.length,
    dvh.length,
    "each 100vh needs a following 100dvh fallback pair",
  );
});
