# Third-party notices

## Geist and Geist Mono

The Web client embeds the Latin subsets of the Geist and Geist Mono variable
fonts in `app/fonts.css` so production builds are deterministic and require no
font download or build cache.

- Upstream project: <https://github.com/vercel/geist-font>
- Copyright: 2024 The Geist Project Authors
- License: SIL Open Font License 1.1 (`licenses/OFL-1.1.txt`)
- Geist Latin source:
  <https://fonts.gstatic.com/s/geist/v5/gyByhwUxId8gMEwcGFWNOITd.woff2>
- Geist Latin decoded SHA-256:
  `9b6f5ff45b278c744b5f379a2c4ecbaf858a842b8eaf82ac8d21b699ca16c608`
- Geist Mono Latin source:
  <https://fonts.gstatic.com/s/geistmono/v6/or3nQ6H-1_WfwkMZI_qYFrcdmhHkjko.woff2>
- Geist Mono Latin decoded SHA-256:
  `5f3d6ad60f29d6cb708414ec6887163d63bf197377ef5417d2483ff31ace6c3b`

The source URLs and subset identities were recovered from the generated
`next/font/google` metadata used by the existing accepted visual baselines.
Only these two decoded font payloads are included in the release.

## KaTeX 0.16.47 and its fonts

Mathematical notation is rendered by KaTeX, and the client serves KaTeX's own
fonts from `client/assets/` -- no CDN, no font download and no network font
request at runtime. Every face below is pinned by digest in
`scripts/katex-fonts.json` and re-derived from the installed package by
`node scripts/katex-font-manifest.mjs --write`; the release payload refuses any
font byte that is not one of these.

- Upstream project: <https://github.com/KaTeX/KaTeX>
- Package: `katex@0.16.47`
- Copyright: 2013-2020 Khan Academy and other contributors
- License: MIT (`licenses/MIT-KaTeX.txt`)
- License SHA-256: `766ccc1f306c885aa45542a9846bbd0a505b27a0374f146778171c2254ce18e3`

| Font component | SHA-256 |
| --- | --- |
| `KaTeX_AMS-Regular.ttf` | `68534840bcfdd2bffb6f0e8deb48684dd01e7f04ea2813267577afb906de1d13` |
| `KaTeX_AMS-Regular.woff` | `30da91e84c893f875e252689faebdc590b2871145e8adc7f9a9d4dbd8ce0b251` |
| `KaTeX_AMS-Regular.woff2` | `0cdd387c9590a1a9f9794560022dbb59654a7d86f187aa0c81495ad42d3a7308` |
| `KaTeX_Caligraphic-Bold.ttf` | `07d8e303ce4fc12b4bb54f1004170dd190a1f3db45d400fe68060df3e0897268` |
| `KaTeX_Caligraphic-Bold.woff` | `1ae6bd7475590e97e7f145a89e09ccde322f7a6bc0b91607b1c8b8ee28290fed` |
| `KaTeX_Caligraphic-Bold.woff2` | `de7701e42cf1f4cf0b766c03fb27977207eee2f4fd5d76fa82188406da43ea4c` |
| `KaTeX_Caligraphic-Regular.ttf` | `ed0b74372feefcbb9c0666b2e210da37b7e49fa7fbbf3eeb11db5f693dacfbb7` |
| `KaTeX_Caligraphic-Regular.woff` | `3398dd02302557a793f2863f88e02d96ce10df2abffa07c8e9fa90775116e65c` |
| `KaTeX_Caligraphic-Regular.woff2` | `5d53e70ad607c2352162dec9e0923fb54ecdafaccbf604cd8dcf7d00facb989b` |
| `KaTeX_Fraktur-Bold.ttf` | `9163df9c7122432e6495b4229fa9071cf9ae86a758ae5efc4924ec2e1a6dbce1` |
| `KaTeX_Fraktur-Bold.woff` | `9be7ceb88004ab8ad124082246fbfcca4091e36385d4ec6ed1df67375dad50fb` |
| `KaTeX_Fraktur-Bold.woff2` | `74444efd593c005e3f4573b44524704c0af0a937fe911cca9e94068d0d140d3f` |
| `KaTeX_Fraktur-Regular.ttf` | `1e6f9579e90e2cac37f8f60a597c436e075c114385652b7cbeb0dec0421291b3` |
| `KaTeX_Fraktur-Regular.woff` | `5e28753be717dac97f559f49bc10be9cf3c124ddcabda6659d11cb68febc6463` |
| `KaTeX_Fraktur-Regular.woff2` | `51814d270d06ff0255dba0799994fa4d8c84d11f09951d47595f4abb1f3602dc` |
| `KaTeX_Main-Bold.ttf` | `138ac28d1663b3037e9c5f52371fa5c63d8324f4a38d22cd573e6ea3a3fd0cf8` |
| `KaTeX_Main-Bold.woff` | `c76c5d696297d51b9cb1639c7da4334f0e7dec81b42b11213b5e25ef671bb822` |
| `KaTeX_Main-Bold.woff2` | `0f60d1b897938ec918c8ce073092411baf9438f6739465693ff18b0f9d20b021` |
| `KaTeX_Main-BoldItalic.ttf` | `70ee1f64a20f2048c21940ef46d0144fd215baa953ca69afd1e31e98544f708f` |
| `KaTeX_Main-BoldItalic.woff` | `a6f7ec0d846ac7ad975adb8959c37ed49b94acbc4ae436db9ce9e20287e4a64c` |
| `KaTeX_Main-BoldItalic.woff2` | `99cd42a3c072d918f2f44984a807cf7aa16e13545fd0875fc07c6c65f99e715b` |
| `KaTeX_Main-Italic.ttf` | `0d85ae7cc30f23790a7f1a58c4a112fdca8aae769b6ba11429af1d98b1b6cb3a` |
| `KaTeX_Main-Italic.woff` | `f1d6ef86f3b11a528bd5185199bd2443ecb2b0dead96d88674b5a2c12be24bdf` |
| `KaTeX_Main-Italic.woff2` | `97479ca6cce906abc961ecac96faa5f9ca2e61b8e7670d475826bcdee9a7c267` |
| `KaTeX_Main-Regular.ttf` | `d0332f52868370fd83ae7fa46470f90c8f2eab2fcf12bc4f88080b340c95a830` |
| `KaTeX_Main-Regular.woff` | `c6368d87e8a1a3a5d337623d83d8dc4b868f242a9ad476237d6f8d1e0f168cdc` |
| `KaTeX_Main-Regular.woff2` | `c2342cd8b869e01752a9321dc17213fc40d4d04c79688c1d43f2cf316abd7866` |
| `KaTeX_Math-BoldItalic.ttf` | `f9377ab0271cda59af24bcffbd46a4d0c8a3572ffafdbb38de2ad5ea7b0d5ee5` |
| `KaTeX_Math-BoldItalic.woff` | `850c0af5c2238497febaf5e461d880bf458c341f42f4f330f1b1ab5698b1998e` |
| `KaTeX_Math-BoldItalic.woff2` | `dc47344dbb6cb5b655c8460d561f4df5f501b90c804ad3c6cec65fe322351ab1` |
| `KaTeX_Math-Italic.ttf` | `08ce98e51b04d58945a301e639e02b6998af29fdfd61a7b8afdd07bbfc479d4a` |
| `KaTeX_Math-Italic.woff` | `8a8d244581371912b8f3f5a23e2437cb2a59cd9bcaebb0346e722c05737a2571` |
| `KaTeX_Math-Italic.woff2` | `7af58c5ec8f132a2ddde9027c6d7814decce4d3b822a11192a42a20e2e973264` |
| `KaTeX_SansSerif-Bold.ttf` | `1ece03f79f95277d57dc7f6b435a74e1379b0d46104a8530286b60ff49369ea0` |
| `KaTeX_SansSerif-Bold.woff` | `ece03cfd83e22c212cdef66feb8442d25a083beb988db3f1883f3f9738d750ba` |
| `KaTeX_SansSerif-Bold.woff2` | `e99ae51144bf1232efcc1bfe5add36262c6866b0faab24fa75740e1b98577a62` |
| `KaTeX_SansSerif-Italic.ttf` | `3931dd81faed86ba021bb2bbdc36f5bed9a38d6b4f4077aca59b265aa1b02083` |
| `KaTeX_SansSerif-Italic.woff` | `91ee67500cc0129aa0ace3ac5c61ff1692102f0f31d02b69347fba35dcb75bf2` |
| `KaTeX_SansSerif-Italic.woff2` | `00b26ac825e2095056396e0553b8ac26d3f8ad158c3826e28b4c45b385c4714a` |
| `KaTeX_SansSerif-Regular.ttf` | `f36ea897e19f4a2e571d1e900e4e3710e438deb05a842486045ba0a3e616a4ad` |
| `KaTeX_SansSerif-Regular.woff` | `11e4dc8a6471ff6d6ee561d53d10fde8f7489e798257ff449c5d37c197435605` |
| `KaTeX_SansSerif-Regular.woff2` | `68e8c73ef42afd3ccec58bf0fba302cce448938e7fc020a5e31f8a952eee1342` |
| `KaTeX_Script-Regular.ttf` | `1c67f068fea8bb09bf099c088b1cf64bd27516a6e07f4684344873564bb66a67` |
| `KaTeX_Script-Regular.woff` | `d96cdf2b3bdd4d64a8fd5f74a4c467f123a8a73931cd435889f08ffaf9bf947a` |
| `KaTeX_Script-Regular.woff2` | `036d4e95149b69ff9bcc0cd55771efeb25ffa3947293e69acd78d5ac328c684b` |
| `KaTeX_Size1-Regular.ttf` | `95b6d2f1a50173bfedb8c63e1d1c99b10427d0a4df4201cb44513b226951a22b` |
| `KaTeX_Size1-Regular.woff` | `c943cc986384f59e86bea5fd7dc50a9c4dfe567a7c05eb40d6790720dead97c9` |
| `KaTeX_Size1-Regular.woff2` | `6b47c40166b6dbe21a5dfca7718413f2147fd2399be1ba605d8ad39cedf25dfe` |
| `KaTeX_Size2-Regular.ttf` | `a6b2099fb555c60e3a0db3a08842ebf1d732c6eb4e4bf44913613bed4fc4e39b` |
| `KaTeX_Size2-Regular.woff` | `2014c523c3210bcc166648c4d4cc57f05b747df07a24277bf71c51e67dc79e3d` |
| `KaTeX_Size2-Regular.woff2` | `d04c54219f9eaec6d4d4fd42dfb28785975a4794d6b2fc71e566b9cd6db842dd` |
| `KaTeX_Size3-Regular.ttf` | `500e04d54f0d51666332c9d2089aa803be22aa878eca539e59fa53c6e522b082` |
| `KaTeX_Size3-Regular.woff` | `6ab6b62e9b62dae2c00dd90f791bd10950be0ecc3490d7d6045f51c2e8fe0949` |
| `KaTeX_Size3-Regular.woff2` | `73d591271b1604960cb10bb90fee021670af7297017e0e98480b332d11f51995` |
| `KaTeX_Size4-Regular.ttf` | `c647367d1dd4e162468717d020e1fc0f1dc5c26ebfdffbe55261713bf88c5877` |
| `KaTeX_Size4-Regular.woff` | `99f9c6750b489c9462bf04900bd3f939df9b829339daaaaa99ef5495cdddea58` |
| `KaTeX_Size4-Regular.woff2` | `a4af7d414440a1c1790825cfb700cf9cf43b0f2c4b04f0ebc523011ad9853ec0` |
| `KaTeX_Typewriter-Regular.ttf` | `f01f3e87d9c6a61c0c081ceb577abd864eb00a612f7ac1620dd6915fad2ef5aa` |
| `KaTeX_Typewriter-Regular.woff` | `e14fed02b1aba7ce9f5afd5844b5d0321b22351febc720e0de8b8723527609f7` |
| `KaTeX_Typewriter-Regular.woff2` | `71d517d67827787cfabdf186914cc3358eda539e37931941f2b2fd4a21f68c0b` |

## Web shell dependencies

The Web shell is built on the assistant-ui registry components and shadcn
primitives. Every package below is a direct dependency of `apps/web` and is
bundled into `client/assets`; no separate license file is shipped for these
MIT/ISC/Apache-2.0 packages because their license text is embedded in the
published package.

## @assistant-ui/react@0.15.18

- Upstream: <https://github.com/assistant-ui/assistant-ui>
- License: MIT

## @assistant-ui/react-markdown@0.14.14

- Upstream: <https://github.com/assistant-ui/assistant-ui>
- License: MIT

## class-variance-authority@0.7.1

- Upstream: <https://github.com/joe-bell/cva>
- License: Apache-2.0

## cn@0.2.6

- Upstream: <https://github.com/shadcn-ui/cn>
- License: MIT

## katex@0.16.47

- Upstream: <https://github.com/KaTeX/KaTeX>
- License: MIT (`licenses/MIT-KaTeX.txt`)

## lucide-react@1.41.0

- Upstream: <https://github.com/lucide-icons/lucide>
- License: ISC

## radix-ui@1.6.7

- Upstream: <https://github.com/radix-ui/primitives>
- License: MIT

## rehype-katex@7.0.1

- Upstream: <https://github.com/remarkjs/remark-math>
- License: MIT

## remark-gfm@4.0.1

- Upstream: <https://github.com/remarkjs/remark-gfm>
- License: MIT

## remark-math@6.0.0

- Upstream: <https://github.com/remarkjs/remark-math>
- License: MIT

## tw-animate-css@1.4.0

- Upstream: <https://github.com/Wombosvideo/tw-animate-css>
- License: MIT

## tw-shimmer@0.4.12

- Upstream: <https://github.com/assistant-ui/assistant-ui>
- License: MIT

## Vendored shadcn base layer

`app/shadcn-tailwind.css` is a byte-for-byte copy of `dist/tailwind.css` from the
`shadcn` npm package, vendored so the CLI package is not a build input. It is the
Tailwind base layer (`@theme inline` keyframes and the `data-*` custom variants)
that `components/ui/*.tsx` paint with.

- Upstream: <https://github.com/shadcn-ui/ui>
- Copied from: `shadcn@4.21.0`, file `dist/tailwind.css`
- License: MIT
- SHA-256: `bc7d83425702955b4cb67cb14ede9d603f9d912376d57a2d81d661094d2a782a`
