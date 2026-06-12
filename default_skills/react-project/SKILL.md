---
name: react-project
description: Use this when creating, restructuring, debugging, or scaling a React/Vite frontend project.
version: 1.0.0
---

# React Project

Use this skill for React/Vite applications, especially multi-page or production-grade frontends.

## Project Shape

- Prefer Vite unless the repo already uses Next.js, Remix, Astro, or another framework.
- Inspect `package.json`, existing `src/`, routing, styling, and component conventions before adding new structure.
- Keep app code under `src/`; use clear boundaries such as `components/`, `pages/` or `routes/`, `hooks/`, `lib/`, `services/`, `state/`, and `styles/`.
- Avoid large monolithic components. Split by user-facing feature or reusable UI primitive.

## Implementation

- Use existing dependencies and design system first.
- For new React code, prefer function components, controlled state, and explicit props.
- Keep data fetching, derived state, and UI rendering separated enough to test and reason about.
- For forms, handle loading, validation, error, disabled, and empty states.
- For async flows, show useful pending/error states and avoid duplicate submissions.

## Styling

- Match the existing styling stack: CSS modules, Tailwind, vanilla CSS, shadcn/ui, MUI, etc.
- Build responsive layouts with stable dimensions and no text overlap.
- Do not add decorative complexity unless it supports the product goal.

## Verification

- Run the repo's obvious checks: `npm run build`, `npm test`, `npm run lint`, or the closest available scripts.
- If adding a dev server, report the local URL and keep it running only when the user needs to inspect it.
- When debugging, reproduce the issue first when practical, then patch and re-run the failing check.
