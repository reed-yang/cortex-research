import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { encodeRgbPng, sha256 } from "./screenshot-contract.mjs";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const iconRoot = path.join(webRoot, "public/icons");
const sizes = [180, 192, 512];
const palette = {
  background: [23, 33, 30],
  panel: [35, 69, 58],
  ink: [255, 253, 247],
  accent: [197, 108, 38],
};

function fillRect(pixels, size, x, y, width, height, color) {
  const x0 = Math.max(0, Math.round(x));
  const y0 = Math.max(0, Math.round(y));
  const x1 = Math.min(size, Math.round(x + width));
  const y1 = Math.min(size, Math.round(y + height));
  for (let row = y0; row < y1; row += 1) {
    for (let column = x0; column < x1; column += 1) {
      const offset = (row * size + column) * 3;
      pixels.set(color, offset);
    }
  }
}

function drawGlyph(pixels, size, rows, x, y, unit, color) {
  rows.forEach((row, rowIndex) => {
    [...row].forEach((cell, columnIndex) => {
      if (cell === "1") {
        fillRect(
          pixels,
          size,
          x + columnIndex * unit,
          y + rowIndex * unit,
          unit * 0.78,
          unit * 0.78,
          color,
        );
      }
    });
  });
}

function createIcon(size) {
  const pixels = Buffer.alloc(size * size * 3);
  for (let offset = 0; offset < pixels.length; offset += 3) {
    pixels.set(palette.background, offset);
  }

  const unit = size / 16;
  fillRect(pixels, size, 2 * unit, 2 * unit, 12 * unit, 12 * unit, palette.panel);
  drawGlyph(
    pixels,
    size,
    ["11111", "10000", "10000", "10000", "10000", "10000", "11111"],
    2.5 * unit,
    4 * unit,
    unit,
    palette.ink,
  );
  drawGlyph(
    pixels,
    size,
    ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    8.5 * unit,
    4 * unit,
    unit,
    palette.ink,
  );
  fillRect(pixels, size, 2.5 * unit, 12 * unit, 11 * unit, 0.8 * unit, palette.accent);

  return encodeRgbPng({ width: size, height: size, stride: size * 3, pixels });
}

const check = process.argv.includes("--check");
assert.ok(check || process.argv.includes("--write"), "Use --write or --check");
await mkdir(iconRoot, { recursive: true });

for (const size of sizes) {
  const filename = `cortex-${size}.png`;
  const target = path.join(iconRoot, filename);
  const generated = createIcon(size);
  if (check) {
    const committed = await readFile(target);
    assert.deepEqual(committed, generated, `${filename} is not reproducible`);
  } else {
    await writeFile(target, generated);
  }
  console.log(`${filename} ${sha256(generated)}`);
}
