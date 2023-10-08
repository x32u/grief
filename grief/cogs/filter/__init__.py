from .filter import Filter
from grief import Red


async def setup(bot: Red) -> None:
    await bot.add_cog(Filter(bot))
