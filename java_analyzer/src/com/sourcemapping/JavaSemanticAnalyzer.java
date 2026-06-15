package com.sourcemapping;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.BodyDeclaration;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.Parameter;
import com.github.javaparser.ast.expr.AnnotationExpr;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.expr.NameExpr;
import com.github.javaparser.ast.stmt.BlockStmt;
import com.github.javaparser.ast.type.ClassOrInterfaceType;
import com.github.javaparser.resolution.declarations.ResolvedMethodDeclaration;
import com.github.javaparser.symbolsolver.JavaSymbolSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.CombinedTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.JavaParserTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.ReflectionTypeSolver;

import java.io.BufferedWriter;
import java.io.IOException;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;
import java.util.stream.Stream;

/**
 * Standalone JavaParser+SymbolSolver analyzer.
 * Reads a source root, builds a project-wide symbol index, emits JSON.
 *
 * Usage: java -cp ... com.sourcemapping.JavaSemanticAnalyzer <sourceRoot> <outputJson>
 */
public class JavaSemanticAnalyzer {

    // -------- layer classification --------
    private static final Set<String> CONTROLLER_ANN = Set.of("RestController", "Controller");
    private static final Set<String> SERVICE_ANN = Set.of("Service");
    private static final Set<String> REPOSITORY_ANN = Set.of("Repository");
    private static final Set<String> MAPPER_ANN = Set.of("Mapper");
    private static final Set<String> CONFIG_ANN = Set.of("Configuration");
    private static final Set<String> COMPONENT_ANN = Set.of("Component");
    private static final Set<String> ENTITY_ANN = Set.of("Entity", "Embeddable", "MappedSuperclass");
    private static final Set<String> ENDPOINT_ANN = Set.of(
            "GetMapping", "PostMapping", "PutMapping", "DeleteMapping",
            "PatchMapping", "RequestMapping");
    private static final Set<String> NOT_IMPL_EXCEPTIONS = Set.of(
            "UnsupportedOperationException", "NotImplementedException");

    // -------- collected state --------
    private final Path sourceRoot;
    private final Map<String, ClassInfo> classes = new LinkedHashMap<>();
    private final Map<String, MethodInfo> methods = new LinkedHashMap<>();
    /** interfaceFqcn -> [implClassFqcn] */
    private final Map<String, Set<String>> interfaceImpls = new HashMap<>();
    /** calleeFqsig -> [callerFqsig] */
    private final Map<String, Set<String>> fanIn = new HashMap<>();

    private int parsedFiles = 0;
    private int parseErrors = 0;
    private int unresolvedCalls = 0;
    private int resolvedCalls = 0;

    public JavaSemanticAnalyzer(Path sourceRoot) {
        this.sourceRoot = sourceRoot;
    }

    // ================= main =================

    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.err.println("Usage: JavaSemanticAnalyzer <sourceRoot> <outputJson>");
            System.exit(2);
        }
        Path sourceRoot = Paths.get(args[0]).toAbsolutePath().normalize();
        Path outputJson = Paths.get(args[1]).toAbsolutePath().normalize();
        if (!Files.isDirectory(sourceRoot)) {
            System.err.println("source root not a directory: " + sourceRoot);
            System.exit(2);
        }

        long t0 = System.currentTimeMillis();
        JavaSemanticAnalyzer analyzer = new JavaSemanticAnalyzer(sourceRoot);
        analyzer.run();
        analyzer.writeJson(outputJson, System.currentTimeMillis() - t0);
        System.out.println("wrote: " + outputJson);
    }

    // ================= pipeline =================

    public void run() throws IOException {
        // Find java source roots — any folder under sourceRoot containing .java is fair game.
        // For SymbolSolver, we register sourceRoot itself plus any nested src/main/java.
        List<Path> roots = discoverSourceRoots(sourceRoot);

        CombinedTypeSolver typeSolver = new CombinedTypeSolver();
        typeSolver.add(new ReflectionTypeSolver());
        for (Path r : roots) typeSolver.add(new JavaParserTypeSolver(r));

        ParserConfiguration config = new ParserConfiguration()
                .setSymbolResolver(new JavaSymbolSolver(typeSolver))
                .setLanguageLevel(ParserConfiguration.LanguageLevel.JAVA_17);
        JavaParser parser = new JavaParser(config);

        // ---- pass 1: parse + record class/method skeleton ----
        List<Path> javaFiles = listJavaFiles(sourceRoot);
        for (Path file : javaFiles) {
            try {
                ParseResult<CompilationUnit> pr = parser.parse(file);
                if (!pr.isSuccessful() || pr.getResult().isEmpty()) {
                    parseErrors++;
                    continue;
                }
                CompilationUnit cu = pr.getResult().get();
                indexCompilationUnit(cu, file);
                parsedFiles++;
            } catch (Exception e) {
                parseErrors++;
            }
        }

        // ---- pass 2: interface→impl mapping ----
        for (ClassInfo c : classes.values()) {
            if (c.isInterface) continue;
            for (String iface : c.implementsFqcn) {
                interfaceImpls.computeIfAbsent(iface, k -> new HashSet<>()).add(c.fqcn);
            }
        }

        // ---- pass 3: resolve method calls + build call graph ----
        for (Path file : javaFiles) {
            try {
                ParseResult<CompilationUnit> pr = parser.parse(file);
                if (!pr.isSuccessful() || pr.getResult().isEmpty()) continue;
                CompilationUnit cu = pr.getResult().get();
                resolveCalls(cu);
            } catch (Exception ignored) {
            }
        }
    }

    private List<Path> discoverSourceRoots(Path root) throws IOException {
        List<Path> roots = new ArrayList<>();
        roots.add(root);
        try (Stream<Path> w = Files.walk(root, 6)) {
            w.filter(Files::isDirectory)
              .filter(p -> p.endsWith(Paths.get("src", "main", "java")))
              .forEach(roots::add);
        }
        return roots;
    }

    private List<Path> listJavaFiles(Path root) throws IOException {
        try (Stream<Path> w = Files.walk(root)) {
            return w.filter(Files::isRegularFile)
                    .filter(p -> p.toString().endsWith(".java"))
                    .collect(Collectors.toList());
        }
    }

    // ================= pass 1: index =================

    private void indexCompilationUnit(CompilationUnit cu, Path file) {
        String relFile = relPath(file);
        cu.findAll(ClassOrInterfaceDeclaration.class).forEach(decl -> {
            String fqcn = decl.getFullyQualifiedName().orElse(null);
            if (fqcn == null) return;

            ClassInfo ci = new ClassInfo();
            ci.fqcn = fqcn;
            ci.simpleName = decl.getNameAsString();
            ci.isInterface = decl.isInterface();
            ci.annotations = annotationNames(decl.getAnnotations());
            ci.layer = classifyLayer(ci, decl);
            ci.file = relFile;
            ci.startLine = decl.getBegin().map(p -> p.line).orElse(-1);
            ci.endLine = decl.getEnd().map(p -> p.line).orElse(-1);

            // extends / implements (best-effort resolve)
            for (ClassOrInterfaceType ext : decl.getExtendedTypes()) {
                String r = tryResolveType(ext);
                ci.extendsFqcn.add(r != null ? r : ext.getNameAsString());
            }
            for (ClassOrInterfaceType impl : decl.getImplementedTypes()) {
                String r = tryResolveType(impl);
                ci.implementsFqcn.add(r != null ? r : impl.getNameAsString());
            }

            classes.put(fqcn, ci);

            for (MethodDeclaration m : decl.getMethods()) {
                MethodInfo mi = buildMethodInfo(m, ci);
                methods.put(mi.fqsig, mi);
                ci.methodFqsigs.add(mi.fqsig);
            }
        });
    }

    private MethodInfo buildMethodInfo(MethodDeclaration m, ClassInfo ci) {
        MethodInfo mi = new MethodInfo();
        mi.name = m.getNameAsString();
        mi.classFqcn = ci.fqcn;
        mi.file = ci.file;
        mi.layer = ci.layer;
        mi.annotations = annotationNames(m.getAnnotations());
        mi.returnType = m.getType().asString();
        mi.startLine = m.getBegin().map(p -> p.line).orElse(-1);
        mi.endLine = m.getEnd().map(p -> p.line).orElse(-1);
        mi.isAbstract = m.isAbstract() || (ci.isInterface && !m.isDefault() && !m.isStatic());
        mi.isDefault = m.isDefault();
        mi.isStatic = m.isStatic();
        mi.hasEndpointAnnotation = mi.annotations.stream().anyMatch(ENDPOINT_ANN::contains);

        // parameters
        for (Parameter p : m.getParameters()) {
            ParamInfo pi = new ParamInfo();
            pi.name = p.getNameAsString();
            pi.type = p.getType().asString();
            pi.used = isParamUsedInBody(p, m);
            mi.parameters.add(pi);
        }
        mi.paramUsageRate = mi.parameters.isEmpty()
                ? 1.0
                : (double) mi.parameters.stream().filter(p -> p.used).count() / mi.parameters.size();

        // try resolve qualified signature; fall back to simple signature
        try {
            ResolvedMethodDeclaration r = m.resolve();
            mi.fqsig = r.getQualifiedSignature();
        } catch (Throwable t) {
            mi.fqsig = ci.fqcn + "." + simpleSig(m);
        }

        // body shape
        if (m.getBody().isEmpty()) {
            mi.bodyShape = mi.isAbstract ? "abstract" : "no_body";
            mi.sloc = 0;
            mi.statementCount = 0;
        } else {
            BlockStmt body = m.getBody().get();
            mi.sloc = countSloc(body);
            mi.statementCount = body.getStatements().size();
            mi.bodyShape = classifyBody(body);
            mi.throwsNotImpl = bodyThrowsNotImpl(body);
        }

        return mi;
    }

    private String classifyLayer(ClassInfo ci, ClassOrInterfaceDeclaration decl) {
        Set<String> anns = new HashSet<>(ci.annotations);
        if (!intersect(anns, CONTROLLER_ANN).isEmpty()) return "CONTROLLER";
        if (!intersect(anns, SERVICE_ANN).isEmpty()) return "SERVICE";
        if (!intersect(anns, REPOSITORY_ANN).isEmpty()) return "REPOSITORY";
        if (!intersect(anns, MAPPER_ANN).isEmpty())
            // @Mapper is shared by two unrelated libraries. MapStruct (org.mapstruct)
            // is compile-time object<->object conversion → UTIL. MyBatis
            // (org.apache.ibatis) is real DB access → MAPPER.
            return isMapStructMapper(decl) ? "UTIL" : "MAPPER";
        if (!intersect(anns, ENTITY_ANN).isEmpty()) return "ENTITY";
        if (!intersect(anns, CONFIG_ANN).isEmpty()) return "CONFIG";
        if (!intersect(anns, COMPONENT_ANN).isEmpty()) return "COMPONENT";

        // Repository by inheritance (Spring Data JPA)
        for (ClassOrInterfaceType ext : decl.getExtendedTypes()) {
            String n = ext.getNameAsString();
            if (n.endsWith("Repository") || n.endsWith("JpaRepository")
                    || n.endsWith("CrudRepository") || n.endsWith("PagingAndSortingRepository")) {
                return "REPOSITORY";
            }
        }
        // Naming convention fallback
        String name = ci.simpleName;
        if (name.endsWith("Controller")) return "CONTROLLER";
        if (name.endsWith("Service") || name.endsWith("ServiceImpl")) return "SERVICE";
        if (name.endsWith("Repository") || name.endsWith("RepositoryImpl") || name.endsWith("Dao") || name.endsWith("DAO"))
            return "REPOSITORY";
        if (name.endsWith("Mapper")) return "MAPPER";
        if (name.endsWith("Util") || name.endsWith("Utils")
                || name.endsWith("Utility") || name.endsWith("Utilities")
                || name.endsWith("Helper") || name.endsWith("Helpers")) return "UTIL";
        if (name.endsWith("Config") || name.endsWith("Configuration")) return "CONFIG";
        if (name.endsWith("Dto") || name.endsWith("DTO") || name.endsWith("Request")
                || name.endsWith("Response") || name.endsWith("Form")) return "DTO";
        return "UNKNOWN";
    }

    private String classifyBody(BlockStmt body) {
        var stmts = body.getStatements();
        if (stmts.isEmpty()) return "empty";
        if (stmts.size() > 1) return "multi_statement";
        var s = stmts.get(0);
        if (s.isThrowStmt()) {
            return bodyThrowsNotImpl(body) ? "stub_throw" : "single_throw";
        }
        if (s.isReturnStmt()) {
            var expr = s.asReturnStmt().getExpression().orElse(null);
            if (expr == null) return "empty_return";
            if (expr.isNullLiteralExpr() || expr.isBooleanLiteralExpr()
                    || expr.isIntegerLiteralExpr() || expr.isStringLiteralExpr()) return "stub_literal";
            if (expr.isMethodCallExpr()) return "delegation";
            if (expr.isFieldAccessExpr() || expr.isNameExpr()) return "accessor";
            return "single_return";
        }
        if (s.isExpressionStmt()) {
            var e = s.asExpressionStmt().getExpression();
            if (e.isMethodCallExpr()) return "delegation";
            if (e.isAssignExpr()) return "accessor";
        }
        return "single_statement";
    }

    private boolean bodyThrowsNotImpl(BlockStmt body) {
        return body.findAll(com.github.javaparser.ast.expr.ObjectCreationExpr.class).stream()
                .anyMatch(o -> NOT_IMPL_EXCEPTIONS.contains(o.getType().getNameAsString()));
    }

    private boolean isParamUsedInBody(Parameter p, MethodDeclaration m) {
        if (m.getBody().isEmpty()) return false;
        String name = p.getNameAsString();
        return m.getBody().get().findAll(NameExpr.class).stream()
                .anyMatch(n -> n.getNameAsString().equals(name));
    }

    private int countSloc(BlockStmt body) {
        int begin = body.getBegin().map(p -> p.line).orElse(0);
        int end = body.getEnd().map(p -> p.line).orElse(0);
        return Math.max(0, end - begin - 1);
    }

    // ================= pass 3: call graph =================

    private void resolveCalls(CompilationUnit cu) {
        cu.findAll(MethodDeclaration.class).forEach(m -> {
            String callerSig = resolveSafe(m);
            if (callerSig == null) return;
            MethodInfo caller = methods.get(callerSig);
            if (caller == null) return;

            if (m.getBody().isEmpty()) return;
            for (MethodCallExpr call : m.getBody().get().findAll(MethodCallExpr.class)) {
                CallInfo ci = new CallInfo();
                ci.name = call.getNameAsString();
                try {
                    ResolvedMethodDeclaration r = call.resolve();
                    ci.targetFqsig = r.getQualifiedSignature();
                    ci.targetClassFqcn = r.declaringType().getQualifiedName();
                    ci.targetLayer = layerOf(ci.targetClassFqcn);
                    ci.resolved = true;
                    resolvedCalls++;
                    fanIn.computeIfAbsent(ci.targetFqsig, k -> new HashSet<>()).add(callerSig);
                } catch (Throwable t) {
                    ci.resolved = false;
                    unresolvedCalls++;
                }
                caller.calls.add(ci);
            }

            // detect delegation target = the single primary call in a 1-liner
            if ("delegation".equals(caller.bodyShape) && !caller.calls.isEmpty()) {
                CallInfo primary = caller.calls.get(caller.calls.size() - 1);
                if (primary.resolved) {
                    caller.delegationTarget = primary.targetFqsig;
                    MethodInfo target = methods.get(primary.targetFqsig);
                    if (target != null) {
                        caller.delegationTargetSloc = target.sloc;
                        caller.delegationTargetLayer = target.layer;
                    }
                }
            }
        });
    }

    private String layerOf(String fqcn) {
        ClassInfo c = classes.get(fqcn);
        return c != null ? c.layer : "EXTERNAL";
    }

    private String resolveSafe(MethodDeclaration m) {
        try {
            return m.resolve().getQualifiedSignature();
        } catch (Throwable t) {
            return m.findAncestor(ClassOrInterfaceDeclaration.class)
                    .flatMap(ClassOrInterfaceDeclaration::getFullyQualifiedName)
                    .map(fqcn -> fqcn + "." + simpleSig(m))
                    .orElse(null);
        }
    }

    private String simpleSig(MethodDeclaration m) {
        String params = m.getParameters().stream()
                .map(p -> p.getType().asString())
                .collect(Collectors.joining(","));
        return m.getNameAsString() + "(" + params + ")";
    }

    private String tryResolveType(ClassOrInterfaceType t) {
        try {
            return t.resolve().describe();
        } catch (Throwable e) {
            return null;
        }
    }

    private List<String> annotationNames(List<AnnotationExpr> anns) {
        return anns.stream().map(a -> a.getNameAsString()).collect(Collectors.toList());
    }

    /** True if the @Mapper on this type is MapStruct (org.mapstruct), not MyBatis. */
    private boolean isMapStructMapper(ClassOrInterfaceDeclaration decl) {
        return decl.findCompilationUnit().map(cu ->
                cu.getImports().stream().anyMatch(imp -> {
                    String n = imp.getNameAsString();
                    // covers `import org.mapstruct.Mapper;` and `import org.mapstruct.*;`
                    return n.equals("org.mapstruct") || n.startsWith("org.mapstruct.");
                })
        ).orElse(false);
    }

    private Set<String> intersect(Set<String> a, Set<String> b) {
        Set<String> r = new HashSet<>(a);
        r.retainAll(b);
        return r;
    }

    private String relPath(Path file) {
        try {
            return sourceRoot.relativize(file).toString().replace('\\', '/');
        } catch (Exception e) {
            return file.toString().replace('\\', '/');
        }
    }

    // ================= JSON output (handwritten) =================

    public void writeJson(Path out, long durationMs) throws IOException {
        Files.createDirectories(out.getParent());
        try (BufferedWriter w = Files.newBufferedWriter(out, StandardCharsets.UTF_8)) {
            J j = new J(w);
            j.openObj();

            j.key("scan").openObj();
            j.kv("source_root", sourceRoot.toString().replace('\\', '/')).comma();
            j.kvNum("parsed_files", parsedFiles).comma();
            j.kvNum("parse_errors", parseErrors).comma();
            j.kvNum("classes", classes.size()).comma();
            j.kvNum("methods", methods.size()).comma();
            j.kvNum("resolved_calls", resolvedCalls).comma();
            j.kvNum("unresolved_calls", unresolvedCalls).comma();
            j.kvNum("duration_ms", durationMs);
            j.closeObj().comma();

            j.key("classes").openArr();
            boolean first = true;
            for (ClassInfo c : classes.values()) {
                if (!first) j.comma();
                first = false;
                writeClass(j, c);
            }
            j.closeArr().comma();

            j.key("methods").openArr();
            first = true;
            for (MethodInfo m : methods.values()) {
                if (!first) j.comma();
                first = false;
                writeMethod(j, m);
            }
            j.closeArr().comma();

            j.key("interface_impls").openObj();
            first = true;
            for (var e : interfaceImpls.entrySet()) {
                if (!first) j.comma();
                first = false;
                j.key(e.getKey()).openArr();
                boolean f2 = true;
                for (String impl : e.getValue()) {
                    if (!f2) j.comma();
                    f2 = false;
                    j.str(impl);
                }
                j.closeArr();
            }
            j.closeObj().comma();

            j.key("fan_in").openObj();
            first = true;
            for (var e : fanIn.entrySet()) {
                if (!first) j.comma();
                first = false;
                j.key(e.getKey()).openArr();
                boolean f2 = true;
                for (String caller : e.getValue()) {
                    if (!f2) j.comma();
                    f2 = false;
                    j.str(caller);
                }
                j.closeArr();
            }
            j.closeObj();

            j.closeObj();
        }
    }

    private void writeClass(J j, ClassInfo c) throws IOException {
        j.openObj();
        j.kv("fqcn", c.fqcn).comma();
        j.kv("simple_name", c.simpleName).comma();
        j.kvBool("is_interface", c.isInterface).comma();
        j.kv("layer", c.layer).comma();
        j.kv("file", c.file).comma();
        j.kvNum("start_line", c.startLine).comma();
        j.kvNum("end_line", c.endLine).comma();
        j.kvNum("method_count", c.methodFqsigs.size()).comma();
        j.keyArrStr("annotations", c.annotations).comma();
        j.keyArrStr("extends", new ArrayList<>(c.extendsFqcn)).comma();
        j.keyArrStr("implements", new ArrayList<>(c.implementsFqcn));
        j.closeObj();
    }

    private void writeMethod(J j, MethodInfo m) throws IOException {
        j.openObj();
        j.kv("fqsig", m.fqsig).comma();
        j.kv("name", m.name).comma();
        j.kv("class_fqcn", m.classFqcn).comma();
        j.kv("file", m.file).comma();
        j.kv("layer", m.layer).comma();
        j.kv("return_type", m.returnType).comma();
        j.kvNum("start_line", m.startLine).comma();
        j.kvNum("end_line", m.endLine).comma();
        j.kvNum("sloc", m.sloc).comma();
        j.kvNum("statement_count", m.statementCount).comma();
        j.kvBool("is_abstract", m.isAbstract).comma();
        j.kvBool("is_default", m.isDefault).comma();
        j.kvBool("is_static", m.isStatic).comma();
        j.kvBool("has_endpoint_annotation", m.hasEndpointAnnotation).comma();
        j.kvBool("throws_not_impl", m.throwsNotImpl).comma();
        j.kv("body_shape", m.bodyShape).comma();
        j.key("param_usage_rate").rawNum(String.format(java.util.Locale.ROOT, "%.3f", m.paramUsageRate)).comma();
        j.keyArrStr("annotations", m.annotations).comma();

        j.key("parameters").openArr();
        boolean first = true;
        for (ParamInfo p : m.parameters) {
            if (!first) j.comma();
            first = false;
            j.openObj();
            j.kv("name", p.name).comma();
            j.kv("type", p.type).comma();
            j.kvBool("used", p.used);
            j.closeObj();
        }
        j.closeArr().comma();

        j.key("calls").openArr();
        first = true;
        for (CallInfo c : m.calls) {
            if (!first) j.comma();
            first = false;
            j.openObj();
            j.kv("name", c.name).comma();
            j.kvBool("resolved", c.resolved);
            if (c.resolved) {
                j.comma().kv("target_fqsig", c.targetFqsig).comma()
                  .kv("target_class_fqcn", c.targetClassFqcn).comma()
                  .kv("target_layer", c.targetLayer);
            }
            j.closeObj();
        }
        j.closeArr();

        if (m.delegationTarget != null) {
            j.comma().kv("delegation_target", m.delegationTarget).comma()
              .kvNum("delegation_target_sloc", m.delegationTargetSloc).comma()
              .kv("delegation_target_layer", m.delegationTargetLayer == null ? "" : m.delegationTargetLayer);
        }
        j.closeObj();
    }

    // ================= data classes =================

    static class ClassInfo {
        String fqcn;
        String simpleName;
        boolean isInterface;
        String layer;
        String file;
        int startLine, endLine;
        List<String> annotations = new ArrayList<>();
        Set<String> extendsFqcn = new HashSet<>();
        Set<String> implementsFqcn = new HashSet<>();
        List<String> methodFqsigs = new ArrayList<>();
    }

    static class MethodInfo {
        String fqsig;
        String name;
        String classFqcn;
        String file;
        String layer;
        List<String> annotations = new ArrayList<>();
        String returnType;
        int startLine, endLine;
        int sloc;
        int statementCount;
        boolean isAbstract;
        boolean isDefault;
        boolean isStatic;
        boolean hasEndpointAnnotation;
        boolean throwsNotImpl;
        String bodyShape;
        double paramUsageRate;
        List<ParamInfo> parameters = new ArrayList<>();
        List<CallInfo> calls = new ArrayList<>();
        String delegationTarget;
        int delegationTargetSloc = -1;
        String delegationTargetLayer;
    }

    static class ParamInfo {
        String name;
        String type;
        boolean used;
    }

    static class CallInfo {
        String name;
        boolean resolved;
        String targetFqsig;
        String targetClassFqcn;
        String targetLayer;
    }

    // ================= mini JSON writer =================

    static class J {
        final BufferedWriter w;
        J(BufferedWriter w) { this.w = w; }
        J openObj() throws IOException { w.write('{'); return this; }
        J closeObj() throws IOException { w.write('}'); return this; }
        J openArr() throws IOException { w.write('['); return this; }
        J closeArr() throws IOException { w.write(']'); return this; }
        J comma() throws IOException { w.write(','); return this; }
        J key(String k) throws IOException { w.write('"'); w.write(esc(k)); w.write("\":"); return this; }
        J kv(String k, String v) throws IOException {
            key(k);
            if (v == null) w.write("null"); else { w.write('"'); w.write(esc(v)); w.write('"'); }
            return this;
        }
        J kvNum(String k, long v) throws IOException { key(k); w.write(Long.toString(v)); return this; }
        J kvBool(String k, boolean v) throws IOException { key(k); w.write(v ? "true" : "false"); return this; }
        J str(String s) throws IOException {
            if (s == null) { w.write("null"); } else { w.write('"'); w.write(esc(s)); w.write('"'); }
            return this;
        }
        J rawNum(String s) throws IOException { w.write(s); return this; }
        J keyArrStr(String k, List<String> items) throws IOException {
            key(k).openArr();
            for (int i = 0; i < items.size(); i++) {
                if (i > 0) comma();
                str(items.get(i));
            }
            closeArr();
            return this;
        }
        static String esc(String s) {
            StringBuilder b = new StringBuilder(s.length() + 8);
            for (int i = 0; i < s.length(); i++) {
                char c = s.charAt(i);
                switch (c) {
                    case '\\': b.append("\\\\"); break;
                    case '"': b.append("\\\""); break;
                    case '\n': b.append("\\n"); break;
                    case '\r': b.append("\\r"); break;
                    case '\t': b.append("\\t"); break;
                    case '\b': b.append("\\b"); break;
                    case '\f': b.append("\\f"); break;
                    default:
                        if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
                        else b.append(c);
                }
            }
            return b.toString();
        }
    }
}
