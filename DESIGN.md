# Terminal interface design

Laintas CLI is an operational workspace for developers. The interface should
make the current task, selected object, result, and next action clear while
preserving shell scrollback. Python, Rich, prompt_toolkit, and the terminal
arbiter remain the implementation boundary.

## Visual language

- Reuse the existing green accent, violet agent/code distinction, and semantic
  warning/error colors. Use the terminal's selected dark, light, or mono mode.
- Use weight, spacing, labels, and selection markers to establish hierarchy.
  Status must remain identifiable without color. Avoid decorative borders and
  movement that makes a list harder to scan.
- Keep command output linear and copyable. Tables retain columns when they fit;
  narrow output becomes labelled records without dropping field values.

## Interactive structure

Every browser has three stable regions: location and state, the working area,
and available actions. Status feedback occupies its own row so it cannot erase
navigation. Help and filters name their current mode and offer a visible return.

- Lists identify the resource; the inspector explains the selected resource.
  Wide viewports show both. Narrow viewports switch between them, preserving
  selection, query, and scroll position on return.
- Searchable choices keep the search field near the title. Long descriptions
  use a selected-item preview instead of competing with the option name.
- Keyboard hints fit complete actions. Escape and the main action take priority;
  contextual help carries secondary shortcuts when space is limited.
- Empty states distinguish an empty collection, an unmatched filter, and a load
  failure. Each gives a relevant recovery action or command.
- Background results belong to the request that started them. Cancellation or
  closing a view prevents late results from replacing the visible context.

## Verification

Review every command and alias, including extension discovery and help. Exercise
actual terminal input, filtering, navigation, modal/help return, resize, live
updates, errors, cancellation, and exit. Include 38/50/80/120-column viewports,
dark/light/mono, and long or wide-character data. Verify exact terminal state
restoration and clean task-owned processes and temporary artifacts.
