# Third-Party Dependencies and Shipped Assets

This directory contains vendored third-party dependencies shipped with Jarvis-Desktop-Agent.

## Vendored Components

### 1. Vimium C (`third_party/vimium_c`)
- **Upstream Repository**: [gdh1995/vimium-c](https://github.com/gdh1995/vimium-c)
- **Version**: 2.12.2
- **License**: Apache-2.0 / MIT (see [LICENSE.txt](vimium_c/LICENSE.txt))
- **Purpose**: Provides keyboard hint overlays and DOM link discovery for browser navigation via Chrome DevTools Protocol (CDP).
- **Modifications**: Bundled as a local extension asset to allow browser navigation without requiring external Chrome Web Store installations.
