"""Shadow-DOM-piercing selectors for FoxESS Lovelace cards.

Both cards use `attachShadow({ mode: "open" })`. Playwright's `>>>`
pierce selector traverses shadow boundaries.

Usage:
    page.locator(ControlCard.SOC_TEXT).text_content()
"""


class ControlCard:
    """Selectors for foxess-control-card."""

    ROOT = "foxess-control-card"
    SOC_TEXT = f"{ROOT} >>> .soc-text"
    DATA_SOURCE = f"{ROOT} >>> .data-source"
    PROGRESS_SECTION = f"{ROOT} >>> .progress-section"
    PROGRESS_LABEL = f"{ROOT} >>> .progress-label"

    # Progress bars (by data-tip-key)
    CHARGE_SOC_BAR = f"{ROOT} >>> .progress-row[data-tip-key='charge-soc']"
    DISCHARGE_SOC_BAR = f"{ROOT} >>> .progress-row[data-tip-key='discharge-soc']"
    CHARGE_FILL = f"{ROOT} >>> .progress-fill.charge-fill"
    DISCHARGE_FILL = f"{ROOT} >>> .progress-fill.discharge-fill"
    PROJECTED_FILL = f"{ROOT} >>> .progress-fill.projected"

    # Status indicators
    DOT_ACTIVE = f"{ROOT} >>> .dot-active"
    DOT_WAITING = f"{ROOT} >>> .dot-waiting"

    # Detail sections
    CHARGE_SECTION = f"{ROOT} >>> .section.charge"
    DISCHARGE_SECTION = f"{ROOT} >>> .section.discharge"
    DETAIL_VALUE = f"{ROOT} >>> .detail-value"


class OverviewCard:
    """Selectors for foxess-overview-card."""

    ROOT = "foxess-overview-card"
    SOLAR_VALUE = f"{ROOT} >>> .node.solar .node-value"
    SOLAR_SUB = f"{ROOT} >>> .node.solar .node-sub"
    HOUSE_NODE = f"{ROOT} >>> .node.house"
    HOUSE_VALUE = f"{ROOT} >>> .node.house .node-value"
    BATTERY_SOC = f"{ROOT} >>> .bat-soc"
    BATTERY_NODE = f"{ROOT} >>> .node.battery"
    GRID_NODE = f"{ROOT} >>> .node.grid"
    GRID_VALUE = f"{ROOT} >>> .node.grid .node-value"
    DATA_SOURCE = f"{ROOT} >>> .data-source"
    DATA_SOURCE_STALE = f"{ROOT} >>> .data-source.stale"
    TITLE = f"{ROOT} >>> .title"
