// Next normally writes these references into next-env.d.ts, which is generated and not committed.
// Repeating them here lets `tsc` (and so CI) type-check `*.module.css` imports on a clean checkout.
/// <reference types="next" />
/// <reference types="next/image-types/global" />
