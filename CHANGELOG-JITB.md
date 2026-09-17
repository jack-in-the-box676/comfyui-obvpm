# JITB Changelog

This changelog documents changes specific to the JITB fork of comfyui-obvpm.

Upstream releases and their changes are documented by the original project.

## v0.2.2-jitb.1

Based on upstream **v0.2.2**.

### Changes

- Enhanced the **Load Image & Crop** node with configurable resolution controls.
- Added editable crop width and height controls.
- Added width and height outputs.
- Added megapixel-based output resizing.
- Added optional output dimension alignment to configurable multiples.
- Added additional named aspect ratio presets.
- Added automatic upscaling when the requested output resolution exceeds the crop resolution.
- Added an **UPSCALING** indicator in the node UI.
- Improved crop dimension stability while moving and resizing the crop area.
- Improved synchronization between crop selection, dimension controls, and output resolution.
- Improved handling of fixed aspect ratios and multiple-aligned dimensions.