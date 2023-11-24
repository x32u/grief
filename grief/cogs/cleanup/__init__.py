from .cleanup import Cleanup
from grief.core.bot import grief


async def setup(bot: grief) -> None:
    await bot.add_cog(Cleanup(bot))
