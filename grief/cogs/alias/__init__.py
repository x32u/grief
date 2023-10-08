from .alias import Alias
from grief import Red


async def setup(bot: Red) -> None:
    await bot.add_cog(Alias(bot))
