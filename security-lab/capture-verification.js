/* SocketStream local-only screencast marker detection.
 * The verification marker has three horizontal bands: magenta, cyan, yellow.
 * A positive match means those pixels are present in the captured frame.
 * A negative match NEVER proves an overlay was excluded by the compositor.
 */
(function (root) {
  "use strict";

  function colorType(r, g, b) {
    if (r >= 165 && g <= 95 && b >= 100 && b > g + 65) return "magenta";
    if (r <= 105 && g >= 135 && b >= 155 && b > r + 65) return "cyan";
    if (r >= 170 && g >= 140 && b <= 110 && r > b + 85) return "yellow";
    return "other";
  }

  function detectMarker(frame) {
    if (!frame || !Number.isInteger(frame.width) || !Number.isInteger(frame.height) ||
        frame.width < 30 || frame.height < 30 || !frame.data ||
        frame.data.length < frame.width * frame.height * 4) {
      throw new TypeError("Expected ImageData-like {width, height, data} frame");
    }
    const width = frame.width;
    const height = frame.height;
    const data = frame.data;
    const stride = width * 4;
    function at(x, y) {
      const i = y * stride + x * 4;
      return colorType(data[i], data[i + 1], data[i + 2]);
    }
    // Scan sampled columns for a contiguous M/C/Y vertical sequence. Require
    // a matching horizontal sample to reject unrelated colorful UI elements.
    const step = Math.max(3, Math.floor(Math.min(width, height) / 180));
    for (let y = 0; y < height - 26; y += step) {
      for (let x = 0; x < width - 26; x += step) {
        if (at(x, y) !== "magenta" || at(x + 18, y) !== "magenta") continue;
        for (let cy = y + 8; cy <= Math.min(y + 130, height - 18); cy += step) {
          if (at(x, cy) !== "cyan" || at(x + 18, cy) !== "cyan") continue;
          for (let yy = cy + 8; yy <= Math.min(cy + 130, height - 1); yy += step) {
            if (at(x, yy) === "yellow" && at(x + 18, yy) === "yellow") {
              return { detected: true, x, y, cyanY: cy, yellowY: yy };
            }
          }
        }
      }
    }
    return { detected: false, x: null, y: null };
  }

  function evaluateComparison({ baseline, protectedFrame, sameStream, monitorSelected, locallyVisible }) {
    if (!baseline || !baseline.detected) {
      return { verdict: "inconclusive", message: "Control frame did not contain the marker. Check that you shared the correct entire monitor and the probe is visible." };
    }
    if (!sameStream || !monitorSelected || !locallyVisible) {
      return { verdict: "inconclusive", message: "Monitor, same-stream, and locally-visible checks are required to compare the two frames." };
    }
    if (!protectedFrame) {
      return { verdict: "inconclusive", message: "Capture an after frame before evaluating exclusion." };
    }
    if (protectedFrame.detected) {
      return { verdict: "visible", message: "Marker pixels remain in the whole-monitor capture. The overlay is NOT excluded from this stream." };
    }
    return { verdict: "candidate", message: "Marker disappeared from this stream. This is a candidate exclusion only: visually review the frames and repeat with an independent recorder." };
  }

  const api = { colorType, detectMarker, evaluateComparison };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.CaptureVerification = api;
})(typeof window !== "undefined" ? window : null);
