import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { deflateSync, inflateSync } from "node:zlib";

const pngSignature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
const expectedScenarios = new Map([
  ["g0-source-conflict.png", "g0"],
  ["g1-successor-lineage.png", "g1"],
]);

export function sha256(buffer) {
  return createHash("sha256").update(buffer).digest("hex");
}

export function validateCaptureSpec(spec) {
  assert.equal(spec.base_origin, "http://127.0.0.1:3000");
  assert.deepEqual(spec.css_viewport, { width: 1600, height: 1000 });
  assert.equal(spec.device_scale_factor, 2);
  assert.equal(spec.capture_scale, 0.5);
  assert.equal(spec.screenshot_mode, "explicit_css_clip");

  const columnNames = ["workspace_rail", "main_workspace", "decision_inbox"];
  for (const name of columnNames) {
    assert.equal(
      spec.rendered_columns_css[name] * spec.device_scale_factor,
      spec.expected_columns_px[name],
    );
  }
  assert.equal(
    columnNames.reduce((sum, name) => sum + spec.expected_columns_px[name], 0),
    spec.css_viewport.width,
  );

  const entries = Object.entries(spec.screenshots);
  assert.deepEqual(
    entries.map(([filename]) => filename).sort(),
    [...expectedScenarios.keys()].sort(),
  );
  const urls = new Set();
  for (const [filename, expected] of entries) {
    assert.equal(expected.scenario, expectedScenarios.get(filename));
    const url = new URL(expected.url);
    assert.equal(url.origin, spec.base_origin);
    assert.equal(url.pathname, "/");
    assert.equal(url.searchParams.get("mode"), "demo");
    assert.equal(url.searchParams.get("capture"), "full");
    assert.equal(url.searchParams.get("scenario"), expected.scenario);
    assert.equal(url.searchParams.size, 3);
    assert.ok(!urls.has(expected.url), "Each screenshot must use an independent scenario URL");
    urls.add(expected.url);
    assert.ok(Array.isArray(expected.expected_dom) && expected.expected_dom.length >= 5);
    assert.ok(Array.isArray(expected.forbidden_dom) && expected.forbidden_dom.length >= 2);
    for (const contract of expected.expected_dom) {
      assert.equal(typeof contract.selector, "string");
      assert.ok(contract.text || contract.exact_text);
      assert.equal(typeof contract.count, "number");
      assert.ok(contract.count > 0);
    }
    assert.deepEqual(expected.clip_css, {
      x: 0,
      y: 0,
      width: expected.width / spec.device_scale_factor,
      height: expected.height / spec.device_scale_factor,
    });
    assert.match(expected.sha256, /^[0-9a-f]{64}$/);
  }
  return entries;
}

function paeth(left, above, upperLeft) {
  const estimate = left + above - upperLeft;
  const leftDistance = Math.abs(estimate - left);
  const aboveDistance = Math.abs(estimate - above);
  const diagonalDistance = Math.abs(estimate - upperLeft);
  return leftDistance <= aboveDistance && leftDistance <= diagonalDistance
    ? left
    : aboveDistance <= diagonalDistance
      ? above
      : upperLeft;
}

export function decodeRgbPng(buffer) {
  assert.ok(buffer.subarray(0, 8).equals(pngSignature), "Screenshot must have a PNG signature");
  let offset = 8;
  let width;
  let height;
  const compressed = [];
  while (offset < buffer.length) {
    const length = buffer.readUInt32BE(offset);
    const type = buffer.toString("ascii", offset + 4, offset + 8);
    const data = buffer.subarray(offset + 8, offset + 8 + length);
    if (type === "IHDR") {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      assert.equal(data[8], 8, "Expected 8-bit PNG output");
      assert.equal(data[9], 2, "Expected RGB PNG output");
      assert.equal(data[12], 0, "Interlaced screenshots are not supported");
    } else if (type === "IDAT") {
      compressed.push(data);
    }
    offset += length + 12;
  }

  assert.ok(width && height && compressed.length, "PNG is missing required chunks");
  const bytesPerPixel = 3;
  const stride = width * bytesPerPixel;
  const filtered = inflateSync(Buffer.concat(compressed));
  const pixels = Buffer.alloc(stride * height);
  let inputOffset = 0;
  for (let y = 0; y < height; y += 1) {
    const filter = filtered[inputOffset];
    inputOffset += 1;
    assert.ok(filter >= 0 && filter <= 4, `Unsupported PNG filter ${filter}`);
    for (let x = 0; x < stride; x += 1) {
      const raw = filtered[inputOffset + x];
      const target = y * stride + x;
      const left = x >= bytesPerPixel ? pixels[target - bytesPerPixel] : 0;
      const above = y > 0 ? pixels[target - stride] : 0;
      const upperLeft = y > 0 && x >= bytesPerPixel ? pixels[target - stride - bytesPerPixel] : 0;
      const predictor =
        filter === 0
          ? 0
          : filter === 1
            ? left
            : filter === 2
              ? above
              : filter === 3
                ? Math.floor((left + above) / 2)
                : paeth(left, above, upperLeft);
      pixels[target] = (raw + predictor) & 255;
    }
    inputOffset += stride;
  }
  return { width, height, pixels, stride };
}

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function pngChunk(type, data) {
  const name = Buffer.from(type, "ascii");
  const chunk = Buffer.alloc(data.length + 12);
  chunk.writeUInt32BE(data.length, 0);
  name.copy(chunk, 4);
  data.copy(chunk, 8);
  chunk.writeUInt32BE(crc32(Buffer.concat([name, data])), data.length + 8);
  return chunk;
}

export function encodeRgbPng({ width, height, pixels, stride = width * 3 }) {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = 2;
  const scanlines = Buffer.alloc((width * 3 + 1) * height);
  for (let y = 0; y < height; y += 1) {
    const target = y * (width * 3 + 1);
    scanlines[target] = 0;
    pixels.copy(scanlines, target + 1, y * stride, y * stride + width * 3);
  }
  return Buffer.concat([
    pngSignature,
    pngChunk("IHDR", header),
    pngChunk("IDAT", deflateSync(scanlines)),
    pngChunk("IEND", Buffer.alloc(0)),
  ]);
}

function regionStats(image, xStart, xEnd, yEnd) {
  let dark = 0;
  let light = 0;
  let count = 0;
  for (let y = 0; y < Math.min(yEnd, image.height); y += 4) {
    for (let x = xStart; x < Math.min(xEnd, image.width); x += 4) {
      const offset = y * image.stride + x * 3;
      const luminance =
        image.pixels[offset] * 0.2126 +
        image.pixels[offset + 1] * 0.7152 +
        image.pixels[offset + 2] * 0.0722;
      if (luminance < 110) dark += 1;
      if (luminance > 180) light += 1;
      count += 1;
    }
  }
  return { darkRatio: dark / count, lightRatio: light / count };
}

export function verifyScreenshotArtifacts(spec, imageBuffers) {
  const entries = validateCaptureSpec(spec);
  for (const [filename, expected] of entries) {
    const buffer = imageBuffers.get(filename);
    assert.ok(buffer, `${filename} is missing`);
    assert.equal(sha256(buffer), expected.sha256, `${filename} content fingerprint drifted`);
    const image = decodeRgbPng(buffer);
    assert.equal(image.width, expected.width, `${filename} width drifted`);
    assert.equal(image.height, expected.height, `${filename} height drifted`);

    const rail = regionStats(image, 0, spec.expected_columns_px.workspace_rail, 900);
    const main = regionStats(
      image,
      spec.expected_columns_px.workspace_rail,
      spec.expected_columns_px.workspace_rail + spec.expected_columns_px.main_workspace,
      900,
    );
    const inbox = regionStats(
      image,
      image.width - spec.expected_columns_px.decision_inbox,
      image.width,
      900,
    );
    assert.ok(rail.darkRatio > 0.7, `${filename} does not contain the dark workspace rail`);
    assert.ok(main.lightRatio > 0.7, `${filename} does not contain the light main workspace`);
    assert.ok(inbox.lightRatio > 0.7, `${filename} does not contain the Decision Inbox rail`);
    assert.ok(inbox.darkRatio > 0.002, `${filename} Decision Inbox appears empty or clipped`);
  }
}
