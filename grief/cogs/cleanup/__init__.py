from .cleanup import Cleanup
from grief.core.bot import Grief


async def setup(bot: Grief) -> None:
    await bot.add_cog(Cleanup(bot))
