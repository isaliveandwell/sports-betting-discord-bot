import os
import requests
import discord
from discord.ext import commands
from discord import ui
from dotenv import load_dotenv

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- Helpers ----------
def decimal_to_american(decimal_odds: float) -> str:
    """Convert decimal odds to American (+/-) format."""
    try:
        decimal_odds = float(decimal_odds)
    except Exception:
        return str(decimal_odds)
    if decimal_odds >= 2.0:
        american = (decimal_odds - 1) * 100
        return f"+{int(round(american))}"
    else:
        american = -100 / (decimal_odds - 1)
        return f"{int(round(american))}"

def market_label(key: str) -> str:
    return {"h2h": "Moneyline", "spreads": "Spread", "totals": "Totals"}.get(key, key)

# Main US leagues (no MLS)
LEAGUES = {
    "🏈 NFL": "americanfootball_nfl",
    "🏈 NCAAF (College Football)": "americanfootball_ncaaf",
    "🏀 NBA": "basketball_nba",
    "🏀 NCAAB (College Basketball)": "basketball_ncaab",
    "⚾ MLB": "baseball_mlb",
    "🏒 NHL": "icehockey_nhl",
    "🥊 UFC / MMA": "mma_mixedmartialarts",
}

ODDS_ENDPOINT = "https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"

# ---------- UI Components ----------
class LeagueDropdown(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=label, value=code)
            for label, code in LEAGUES.items()
        ]
        super().__init__(
            placeholder="Select a League",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        # public message
        await interaction.response.defer(thinking=True, ephemeral=False)

        league_code = self.values[0]
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "us",
            "markets": "h2h,spreads,totals",
            "oddsFormat": "decimal",
        }
        try:
            resp = requests.get(
                ODDS_ENDPOINT.format(sport_key=league_code),
                params=params,
                timeout=12,
            )
            resp.raise_for_status()
            games = resp.json()
        except Exception as e:
            await interaction.followup.send(f"❌ API error: {e}", ephemeral=False)
            return

        if not games:
            await interaction.followup.send("❌ No games found today for that league.", ephemeral=False)
            return

        # Build Game dropdown (Discord limit: 25)
        view = ui.View(timeout=120)
        view.add_item(GameDropdown(games))
        note = " (showing first 25)" if len(games) > 25 else ""
        await interaction.followup.send(f"Select a game{note}:", view=view, ephemeral=False)

class GameDropdown(ui.Select):
    def __init__(self, games):
        self.games = games
        options = []
        for g in games[:25]:
            label = f"{g['away_team']} @ {g['home_team']}"
            options.append(discord.SelectOption(label=label, value=g["id"]))
        super().__init__(
            placeholder="Select a Game",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        game_id = self.values[0]
        game = next((g for g in self.games if g["id"] == game_id), None)
        if not game:
            await interaction.response.send_message("❌ Game not found.", ephemeral=False)
            return

        view = ui.View(timeout=120)
        view.add_item(MarketDropdown(game))
        await interaction.response.send_message("Select a market:", view=view, ephemeral=False)

class MarketDropdown(ui.Select):
    def __init__(self, game):
        self.game = game
        options = [
            discord.SelectOption(label="Moneyline", value="h2h"),
            discord.SelectOption(label="Spread", value="spreads"),
            discord.SelectOption(label="Totals", value="totals"),
        ]
        super().__init__(
            placeholder="Select a Market",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        mkey = self.values[0]
        bookmakers = self.game.get("bookmakers", [])
        lines = []

        # Collect outcomes for selected market
        for book in bookmakers:
            mk = next((m for m in book.get("markets", []) if m.get("key") == mkey), None)
            if not mk:
                continue
            for outcome in mk.get("outcomes", []):
                lines.append({
                    "book": book.get("title", "?"),
                    "name": outcome.get("name"),
                    "price_dec": outcome.get("price"),
                    "american": decimal_to_american(outcome.get("price")),
                    "point": outcome.get("point"),
                })

        if not lines:
            await interaction.response.send_message("❌ No odds found for that market.", ephemeral=False)
            return

        away = self.game.get("away_team", "Away")
        home = self.game.get("home_team", "Home")
        header = f"📊 {away} @ {home} — {market_label(mkey)}"

        # Format by market
        if mkey == "h2h":
            by_team = {}
            for e in lines:
                by_team.setdefault(e["name"], []).append(e)

            msg = [header]
            for team, lst in by_team.items():
                lst_sorted = sorted(lst, key=lambda x: (x["price_dec"] or 0), reverse=True)
                section = [f"\n**{team}** (high → low):"]
                for e in lst_sorted:
                    section.append(f"{e['book']}: {e['american']}")
                msg.append("\n".join(section))
            text = "\n".join(msg)

        elif mkey == "spreads":
            by_team = {}
            for e in lines:
                by_team.setdefault(e["name"], []).append(e)

            msg = [header]
            for team, lst in by_team.items():
                lst_sorted = sorted(lst, key=lambda x: (x["price_dec"] or 0), reverse=True)
                section = [f"\n**{team}** (spread high → low):"]
                for e in lst_sorted:
                    pt = e["point"]
                    pt_str = f"{pt:+g}" if pt is not None else ""
                    section.append(f"{e['book']}: {pt_str} {e['american']}")
                msg.append("\n".join(section))
            text = "\n".join(msg)

        else:  # totals
            by_kind = {"Over": [], "Under": []}
            for e in lines:
                name = e["name"]
                by_kind.setdefault(name, []).append(e)

            msg = [header]
            for kind in ("Over", "Under"):
                lst = by_kind.get(kind, [])
                if not lst:
                    continue
                lst_sorted = sorted(lst, key=lambda x: (x["price_dec"] or 0), reverse=True)
                section = [f"\n**{kind}** (high → low):"]
                for e in lst_sorted:
                    pt = e["point"]
                    pt_str = f"{pt:g}" if pt is not None else ""
                    section.append(f"{e['book']}: {pt_str} {e['american']}")
                msg.append("\n".join(section))
            text = "\n".join(msg)

        await interaction.response.send_message(text, ephemeral=False)

# ---------- Slash Command ----------
@bot.tree.command(name="odds", description="League → Game → Market dropdowns (American odds)")
async def odds_cmd(interaction: discord.Interaction):
    view = ui.View(timeout=120)
    view.add_item(LeagueDropdown())
    await interaction.response.send_message("Select a league:", view=view, ephemeral=False)

# ---------- Auto-sync to every guild the bot is in ----------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    # Try global sync (may take time to propagate, but harmless)
    try:
        await bot.tree.sync()
        print("🌍 Global commands synced.")
    except Exception as e:
        print(f"Global sync error: {e}")
    # Instant sync in each guild where the bot is already a member
    for g in bot.guilds:
        try:
            synced = await bot.tree.sync(guild=discord.Object(id=g.id))
            print(f"🔁 Synced {len(synced)} command(s) in {g.name} ({g.id})")
        except Exception as e:
            print(f"Guild sync error for {g.id}: {e}")

bot.run(DISCORD_TOKEN)

