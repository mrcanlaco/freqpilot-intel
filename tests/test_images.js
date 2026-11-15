"use strict";
// Pure URL fixtures only: no DOM, network, CDN requests or production captures.
const {test} = require("node:test");
const assert = require("node:assert/strict");
require("../src/bti/static/images.js");
const {safeURL, urls} = BTIImages;
const cdn = "https://public.bnbstatic.com";
const image = name => `${cdn}/image/${name}.png`;

test("accepts both observed Binance image path forms and preserves original URL bytes", () => {
  const accepted = [
    `${cdn}/image/2026/11/chart.jpg`,
    `${cdn}/static/content/square/images/cover.png`,
    `${cdn}/image/chart.jpeg`, `${cdn}/image/chart.webp`, `${cdn}/image/chart.gif`,
    `${cdn}/image/CHART.JPEG`, `${cdn}/image/CHART.PNG`, `${cdn}/image/chart.WeBp`,
    "HTTPS://PUBLIC.BNBSTATIC.COM:443/image/chart.JPG?width=1200&quality=80",
    `${cdn}:443/static/content/square/images/cover.png?token=a%2Fb%3D&width=800`,
    `${cdn}/image/a%20chart.png?name=one%20two&key=a%23b`,
    `${cdn}/image/chart.png?next=https%3A%2F%2Fexample.org%2Fimage.jpg`
  ];
  for (const value of accepted) assert.equal(safeURL(value), value, value);
});

test("rejects wrong types, whitespace, control characters, fragments and excess length", () => {
  for (const value of [undefined, null, 1, true, {}, [], new String(image("one")), "",
    ` ${image("one")}`, `${image("one")} `, `${cdn}/image/a b.png`,
    `${image("one")}#`, `${image("one")}#cover`, `${image("one")}?x=#fragment`,
    `${cdn}/image/a\nb.png`, `${cdn}/image/a\tb.png`, `${image("one")}?x=\u0000`,
    `${image("one")}?x=\u007f`, `${image("one")}?x=\u0085`, `${image("one")}?x=\u00a0`
  ]) assert.equal(safeURL(value), null, String(value));
  const prefix = `${image("one")}?q=`;
  const boundary = prefix + "a".repeat(2048 - prefix.length);
  assert.equal(safeURL(boundary), boundary);
  assert.equal(safeURL(boundary + "a"), null);
});

test("rejects unsafe origins, alternate ports, credentials and authority parser tricks", () => {
  const rejected = [
    "http://public.bnbstatic.com/image/chart.png", "//public.bnbstatic.com/image/chart.png",
    "data:image/png;base64,AAAA", "javascript:alert(1)", "file:///image/chart.png",
    "https://example.com/image/chart.png", "https://bnbstatic.com/image/chart.png",
    "https://public.bnbstatic.com.example.com/image/chart.png",
    "https://sub.public.bnbstatic.com/image/chart.png",
    "https://public.bnbstatic.com./image/chart.png",
    "https://public%2ebnbstatic.com/image/chart.png",
    "https://user@public.bnbstatic.com/image/chart.png",
    "https://user:pass@public.bnbstatic.com/image/chart.png",
    "https://@public.bnbstatic.com/image/chart.png",
    "https://public.bnbstatic.com@evil.example/image/chart.png",
    "https://public.bnbstatic.com:80/image/chart.png",
    "https://public.bnbstatic.com:8443/image/chart.png",
    "https://public.bnbstatic.com:/image/chart.png",
    "https://public.bnbstatic.com:0443/image/chart.png",
    "https:///public.bnbstatic.com/image/chart.png",
    "https://public.bnbstatic.com\\evil.example/image/chart.png",
    "https://public.bnbstatic.com/image/a\\chart.png"
  ];
  for (const value of rejected) assert.equal(safeURL(value), null, value);
});

test("limits paths to case-sensitive source prefixes and genuine raster suffixes", () => {
  const paths = [
    "/avatar/chart.png", "/images/chart.png", "/Image/chart.png",
    "/STATIC/content/square/images/chart.png", "/static/content/square/image/chart.png",
    "/image", "/image/", "/static/content/square/images/",
    "/image/chart.svg", "/image/chart.SVG", "/image/chart.avif", "/image/chart.html",
    "/image/chart.png.svg", "/image/chart.png/",
    "/image/chart?format=png", "/image/chart.svg?filename=chart.png",
    "/image/chart%2epng", "/image%2fchart.png", "/image/chart.png%00",
    "/image/../avatar/chart.png", "/image/./chart.png", "/image/folder/../chart.png",
    "/image/%2e/chart.png", "/image/%2E%2E/chart.png", "/image/.%2e/chart.png",
    "/static/content/square/images/%2e./chart.png", "/image/folder%2fchart.png",
    "/image/folder%5cchart.png"
  ];
  for (const path of paths) {
    assert.equal(safeURL(cdn + path), null, path);
  }
  assert.equal(safeURL(`${cdn}/image/chart.png?format=png`), `${cdn}/image/chart.png?format=png`);
});

test("returns ordered exact-string deduplication, bounded by nine valid URLs not input positions", () => {
  const first = image("first"), second = image("second");
  const query = first + "?width=600";
  const values = [null, "https://example.com/image/x.png", first, first, false, second, query, second,
    ...Array.from({length: 14}, (_, index) => image(`extra-${index}`))];
  const expected = [first, second, query, ...Array.from({length: 6}, (_, index) => image(`extra-${index}`))];
  assert.deepEqual(urls(values), expected);
  assert.deepEqual(urls({images: values, avatar: image("avatar")}), expected);
  assert.equal(values.length, 22);
  assert.deepEqual(urls([first, `${cdn}:443/image/first.png`, first]), [first, `${cdn}:443/image/first.png`]);
  const result = urls(values);
  result.pop();
  assert.deepEqual(urls(values), expected);
});

test("does not infer images from unrelated fields or coerce malformed input", () => {
  for (const value of [undefined, null, false, 0, "", image("one"), {}, {images: image("one")},
    {images: null}, {images: {0: image("one"), length: 1}},
    {cover: image("cover"), avatar: image("avatar"), content: `<img src="${image("one")}">`}]) {
    assert.deepEqual(urls(value), []);
  }
  assert.deepEqual(urls([image("one"), {url: image("two")}, [image("three")]]), [image("one")]);
  assert.deepEqual(urls(Array(30).fill("https://example.com/image/bad.png").concat(image("last"))), [image("last")]);
});
