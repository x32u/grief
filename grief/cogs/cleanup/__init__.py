from .cleanup import Cleanup
from grief import Red


async def setup(bot: Red) -> None:
    await bot.add_cog(Cleanup(bot))
