# Gental MCP for Odoo

`gental_mcp` adds an [MCP](https://modelcontextprotocol.io) server to your Odoo
database at `/mcp`. Gental, Claude, ChatGPT and any other MCP client can sign in
through your Odoo login and work in Odoo **as that user**: they see and change
only what that user is allowed to, and every call is logged in an Inbox you can
review.

Made by [ignteQ](https://gental.ai). Free and open source (LGPL-3).

- Product page: <https://gental.ai/en/integrations/odoo/>
- Setup guide: <https://gental.ai/en/guides/connect-odoo-mcp/>

## Supported versions

One branch per Odoo version: `17.0`, `18.0`, `19.0`, `20.0`. Community and
Enterprise, on-premise and Odoo.sh. **Odoo Online is not supported**, because it
does not allow custom modules.

## Install

From the Odoo App Store, search for "MCP Server for AI Agents", or from this repo:

```bash
git clone --depth 1 -b 19.0 https://github.com/ignteqorg/gental-odoo.git
# copy or link gental_mcp/ into a folder listed in addons_path, then:
#   restart Odoo, enable developer mode, Apps > Update Apps List,
#   search "Gental MCP" and install
```

Use the branch that matches your Odoo version. The module depends only on `base`,
`web` and `mail`.

## Set up

1. Install the module and open **Gental MCP** from the Odoo menu.
2. Set the Public URL (your Odoo address, https) in Configuration. Run Odoo with
   `proxy_mode = True` behind a reverse proxy.
3. Add the address `https://<your-odoo>/mcp` as a connector in your AI assistant
   and sign in with your Odoo login, or create an API key for a service client.

The full walkthrough, with screenshots, is in the
[setup guide](https://gental.ai/en/guides/connect-odoo-mcp/).

## Security model

- OAuth 2.1 with PKCE (S256), short-lived access tokens, rotating refresh tokens
  with reuse detection.
- Every tool runs with the signed-in user's own Odoo access rights and record
  rules. Business data is never read through elevated access.
- Tokens, keys and secrets are stored only as SHA-256 hashes; an API key is shown
  once.
- Rate limits per client and per IP, size and time limits per call, and a
  reviewable call log with 90 days of retention.

## Licence

[LGPL-3](LICENSE). The GPL-3 text it builds on is in [COPYING](COPYING).

## Support

Email <hello@gental.ai>.

## Issues and contributions

This repository is a read-only mirror: each branch is one commit per release,
published from ignteQ's private source. You are welcome to open issues and pull
requests here. We review them and port accepted changes back into the source, so
a pull request is never merged here directly; it lands in the next release.

---

<div dir="rtl">

## جنتال MCP لأودو

وحدة `gental_mcp` تضيف خادم MCP لقاعدة بيانات أودو على المسار `/mcp`. يقدر
مساعدك الذكي (جنتال، Claude، ChatGPT أو أي عميل MCP) يسجّل دخوله بحساب أودو حقك
ويشتغل **بصلاحيات المستخدم نفسه** بالضبط، وكل استدعاء ينسجّل في سجل تقدر تراجعه.

- دليل الإعداد: <https://gental.ai/ar/guides/connect-odoo-mcp/>
- الترخيص: LGPL-3
- الدعم: <hello@gental.ai>

كل نسخة من أودو لها فرع خاص فيها (`17.0` و`18.0` و`19.0` و`20.0`). أودو أونلاين
غير مدعوم لأنه ما يسمح بالوحدات المخصصة.

</div>
