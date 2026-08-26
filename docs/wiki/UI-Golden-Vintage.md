# Golden Vintage UI Design System

## 🎨 Aesthetic Intent

AUDAPACK adheres to the **Golden Vintage** design system — an authentic dark golden interpretation of classic Windows 95 UI geometry. It delivers maximum information density, clear contrast, and zero eye fatigue during extended coding sessions.

---

## 🎨 Color Palette Tokens (Golden Default)

| Token Name | Hex Code | RGB | Role / Surface |
|:---|:---|:---|:---|
| `borderDark` | `#100E08` | `rgb(16, 14, 8)` | Outermost dark border & deep sunken bevel |
| `surface` | `#332E22` | `rgb(51, 46, 34)` | Primary window, container & slot background |
| `surfaceElevated` | `#3D372A` | `rgb(61, 55, 42)` | Button normal state & elevated panel |
| `bevelLight` | `#75663D` | `rgb(117, 102, 61)` | Inner highlight bevel on raised elements |
| `borderMuted` | `#5A5040` | `rgb(90, 80, 64)` | Subtle separator borders and grid lines |
| `textSecondary` | `#9C9371` | `rgb(156, 147, 113)` | Secondary labels, paths, and slot numbers |
| `textPrimary` | `#D4C89A` | `rgb(212, 200, 154)` | Primary text, project titles, and button text |
| `borderHighlight`| `#F0D060` | `rgb(240, 208, 96)` | Active focus border, gold accents & hot items |

---

## 📐 Geometric Rules

1. **2px Physical Bevels**:
   - **Raised Elements (Buttons, Panels)**: Top/Left = `bevelLight` (`#75663D`), Bottom/Right = `borderDark` (`#100E08`).
   - **Sunken Elements (Inputs, Status Bars)**: Top/Left = `borderDark` (`#100E08`), Bottom/Right = `bevelLight` (`#75663D`).
2. **Zero Antialiasing (No AA)**:
   - Fonts and brand icons are rendered with crisp pixel thresholds. Zero subpixel blurring or fuzzy interpolation.
3. **Fixed Grid Proportions**:
   - Table columns adhere to fixed pixel widths ensuring zero horizontal layout shifting during real-time status updates.
