# Product verification regression cases

This tiny text indexer has three deliberately broken variants that still exit zero
and print a success message. A product check must reject missing output, empty
output and incorrect content. `tests/test_product_verification.py` executes every
case in `suite.json` against the trusted probe, with no model calls.

This measures whether SwarmForge rejects these known failures. It does not prove
Whisper, YOLO, CUDA, browser playback or real video accuracy. Projects needing
those capabilities must supply real application checks in their own contract.
