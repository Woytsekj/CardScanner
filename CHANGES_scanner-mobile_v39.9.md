# CardScanner Mobile v39.9 — diagonal card handling

## Changes
- Estimates diagonal card rotation from downscaled edge directions before OCR.
- Tries an expanded, non-clipping rotated canvas only when a clear skew peak is present.
- Compares the rotated card-boundary score with the original detector and accepts the correction only when the result is at least as plausible.
- Runs existing footer-first, title, and local catalog recognition on the deskewed image, increasing the chance of reading cards that land diagonally.
- Reports the correction angle in the prediction banner for debugging.
- Bumps the PWA service-worker cache and application version.

## Limitations
- Corrects in-plane rotation (a card turned clockwise/counter-clockwise). It does not perform full perspective/keystone rectification for a card viewed at a steep angle.
- Image art and a patterned background can create ambiguous edge directions; the correction is accepted only if card-boundary detection improves. Manual printing confirmation remains required.

## Validation
- Python syntax compilation and mobile JavaScript syntax checks passed.
- The orientation estimator was checked against upright and synthetically rotated card imagery; on-device accuracy and latency still need testing with the user's scanner chute.
