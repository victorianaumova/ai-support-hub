# Product

Design context for the web app (`apps/web/`). Strategy only; the look and tokens are in [UI §7](ui.md#7-design-tokens). Source of truth for users and goals: the [PRD](prd.md).

## Register

product

## Users

- **Support operator** — works the queue all day; needs the draft, sources, account facts and the reason for review in one screen, fast and without mistakes about money.
- **Support lead** — checks quality, automation share and cost; switches auto-reply on or off.
- **Compliance officer** — reads the audit log and verifies the chain.
- **Customer (demo)** — a freelancer or small business with a money problem, in an in-app chat.

## Product Purpose

AI support for Tallybridge, a fictional payments company: answer routine tickets with cited sources and hand anything risky to a human, with good preparation. Success is correct answers fast, never a wrong answer about money.

## Brand Personality

Calm, friendly, trustworthy. Soft and approachable rather than corporate-cold; quiet confidence for a product that handles people's money. Delight is saved for small moments (welcome line, empty states), never in the way of the task.

## Anti-references

- Dense grey enterprise admin consoles that feel like a database front end.
- Neon dark "AI" dashboards, glassmorphism, gradient text.
- Navy-and-gold "bank" clichés.

## Design Principles

1. **The task first.** Every screen has one main job (approve a draft, find a ticket, read a number); decoration never competes with it.
2. **Fail towards a human, visibly.** Risky states (urgent, over SLA, handoff) are the loudest things on screen, always with a word, never colour alone.
3. **Soft surface, firm facts.** A gentle, friendly frame around exact, unambiguous data.
4. **One vocabulary.** Same buttons, pills and tables on every page.

## Accessibility & Inclusion

WCAG 2.1 AA (axe runs in e2e). Works at 360 px and up, keyboard reachable, visible focus, colour never the only signal, reduced motion respected.
