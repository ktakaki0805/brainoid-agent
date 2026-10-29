import com.sun.source.util.JavacTask;
import javax.tools.ToolProvider;
import java.util.List;

/** Parse Java syntax without pretending Android SDK symbol/type checking took place. */
public final class ParseJava {
    public static void main(String[] args) throws Exception {
        var compiler = ToolProvider.getSystemJavaCompiler();
        try (var files = compiler.getStandardFileManager(null, null, null)) {
            final int[] errors = { 0 };
            var task = (JavacTask) compiler.getTask(null, files, diagnostic -> {
                if (diagnostic.getKind() == javax.tools.Diagnostic.Kind.ERROR) errors[0]++;
            }, List.of("-proc:none"), null, files.getJavaFileObjects(args));
            task.parse();
            if (errors[0] != 0) throw new AssertionError("Java syntax errors: " + errors[0]);
            System.out.println("Android plugin syntax parsed; SDK symbol/type checks not performed");
        }
    }
}
