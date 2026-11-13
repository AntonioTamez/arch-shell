using System.Reflection;

// Tracer: the analyzer only reports its version. Roslyn analysis arrives in a later story.
var version = typeof(Program).Assembly
    .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?
    .InformationalVersion
    .Split('+')[0] ?? "0.0.0";

Console.Out.Write($"archshell-analyzer {version}\n");
return 0;
