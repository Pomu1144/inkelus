import sys

if "--web" in sys.argv[1:]:
    from .server import main

    main([a for a in sys.argv[1:] if a != "--web"])
else:
    from .app import main

    main()
