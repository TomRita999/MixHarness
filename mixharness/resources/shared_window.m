#import <AppKit/AppKit.h>
#import <ApplicationServices/ApplicationServices.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>
#import <math.h>
#import <unistd.h>

static void fail(NSString *reason) __attribute__((noreturn));
static void fail(NSString *reason) {
    fprintf(stderr, "%s\n", reason.UTF8String);
    exit(1);
}

static void reply(id value) __attribute__((noreturn));
static void reply(id value) {
    NSError *error = nil;
    NSData *data = [NSJSONSerialization dataWithJSONObject:value options:0 error:&error];
    if (!data) fail(error.localizedDescription);
    if (fwrite(data.bytes, 1, data.length, stdout) != data.length) exit(1);
    exit(0);
}

static NSArray *bounds(CGRect rect) {
    return @[@(rect.origin.x), @(rect.origin.y), @(rect.size.width), @(rect.size.height)];
}

static NSString *windowKey(SCWindow *window) {
    pid_t pid = window.owningApplication.processID;
    if (!window.isOnScreen || !window.windowID || window.windowLayer != 0 ||
        pid <= 0 || pid == getppid() || CGRectIsEmpty(window.frame)) return nil;
    NSRunningApplication *owner = [NSRunningApplication runningApplicationWithProcessIdentifier:pid];
    if (!owner.launchDate || owner.activationPolicy != NSApplicationActivationPolicyRegular ||
        [owner.bundleIdentifier isEqualToString:@"local.mixharness.desktop"]) return nil;
    long long birth = llround(owner.launchDate.timeIntervalSince1970 * 1000000);
    return [NSString stringWithFormat:@"%d-%lld-%u", pid, birth, window.windowID];
}

static NSDictionary *descriptionOf(SCWindow *window, NSString *key) {
    return @{@"target":key, @"app":window.owningApplication.applicationName ?: @"",
             @"title":window.title ?: @""};
}

static void checkLiveWindow(SCWindow *window, NSString *key) {
    NSArray *records = CFBridgingRelease(CGWindowListCopyWindowInfo(kCGWindowListOptionIncludingWindow, window.windowID));
    NSDictionary *item = records.firstObject;
    NSDictionary *position = item[(__bridge NSString *)kCGWindowBounds];
    CGRect frame;
    if (![windowKey(window) isEqualToString:key] || ![item[(__bridge NSString *)kCGWindowIsOnscreen] boolValue] ||
        [item[(__bridge NSString *)kCGWindowNumber] unsignedIntValue] != window.windowID ||
        [item[(__bridge NSString *)kCGWindowOwnerPID] intValue] != window.owningApplication.processID ||
        !position || !CGRectMakeWithDictionaryRepresentation((__bridge CFDictionaryRef)position, &frame) ||
        !CGRectEqualToRect(frame, window.frame)) fail(@"共享窗口已关闭、隐藏或移动，请重新读取窗口。");
}

static void capture(SCWindow *window, NSString *key) API_AVAILABLE(macos(14.2));
static void capture(SCWindow *window, NSString *key) {
    SCContentFilter *filter = [[SCContentFilter alloc] initWithDesktopIndependentWindow:window];
    SCStreamConfiguration *config = [[SCStreamConfiguration alloc] init];
    CGSize size = window.frame.size;
    double scale = fmin(1.0, 1600.0 / fmax(size.width, size.height));
    config.width = MAX(1, lround(size.width * scale));
    config.height = MAX(1, lround(size.height * scale));
    config.showsCursor = NO;
    config.ignoreShadowsSingleWindow = YES;
    config.includeChildWindows = NO;
    [SCScreenshotManager captureImageWithFilter:filter configuration:config completionHandler:^(CGImageRef image, NSError *error) {
        if (!image || error) fail(error.localizedDescription ?: @"无法读取共享窗口画面。");
        checkLiveWindow(window, key);
        NSBitmapImageRep *bitmap = [[NSBitmapImageRep alloc] initWithCGImage:image];
        NSData *jpeg = [bitmap representationUsingType:NSBitmapImageFileTypeJPEG properties:@{NSImageCompressionFactor:@0.88}];
        if (!jpeg.length) fail(@"无法编码窗口画面。");
        reply(@{@"width":@(CGImageGetWidth(image)), @"height":@(CGImageGetHeight(image)),
                @"bounds":bounds(window.frame), @"jpeg":[jpeg base64EncodedStringWithOptions:0]});
    }];
}

static id attribute(id element, CFStringRef name) {
    if (!element) return nil;
    CFTypeRef value = NULL;
    AXError error = AXUIElementCopyAttributeValue((__bridge AXUIElementRef)element, name, &value);
    if (error == kAXErrorAttributeUnsupported || error == kAXErrorNoValue) return nil;
    if (error != kAXErrorSuccess) fail(@"无法读取控件的辅助功能属性，请重新读取窗口。");
    return CFBridgingRelease(value);
}

static BOOL hasFrame(id element, CGRect frame) {
    id position = attribute(element, kAXPositionAttribute);
    id size = attribute(element, kAXSizeAttribute);
    CGPoint origin;
    CGSize extent;
    return position && size && CFGetTypeID((__bridge CFTypeRef)position) == AXValueGetTypeID() &&
           CFGetTypeID((__bridge CFTypeRef)size) == AXValueGetTypeID() &&
           AXValueGetValue((__bridge AXValueRef)position, kAXValueCGPointType, &origin) &&
           AXValueGetValue((__bridge AXValueRef)size, kAXValueCGSizeType, &extent) &&
           CGPointEqualToPoint(origin, frame.origin) && CGSizeEqualToSize(extent, frame.size);
}

static id accessibilityWindow(id app, SCWindow *window, NSArray<SCWindow *> *windows) {
    // 公开的辅助功能接口没有统一的窗口编号；两个接口都必须能唯一确定同一个窗口。
    for (SCWindow *other in windows) {
        if (other.windowID != window.windowID && other.owningApplication.processID == window.owningApplication.processID &&
            CGRectEqualToRect(other.frame, window.frame)) fail(@"此应用有重叠且尺寸相同的窗口，请移动共享窗口后重试。");
    }
    id match = nil;
    for (id candidate in attribute(app, kAXWindowsAttribute)) {
        if (!hasFrame(candidate, window.frame)) continue;
        if (match) fail(@"多个辅助功能窗口符合条件，已取消操作。");
        match = candidate;
    }
    if (!match) fail(@"此应用未提供可与共享窗口匹配的辅助功能窗口。");
    return match;
}

static BOOL belongs(id element, id window) {
    return element && ([element isEqual:window] || [attribute(element, kAXWindowAttribute) isEqual:window]);
}

static void setAttribute(id element, CFStringRef name, id value) {
    AXError error = AXUIElementSetAttributeValue((__bridge AXUIElementRef)element, name, (__bridge CFTypeRef)value);
    if (error != kAXErrorSuccess) fail(@"此控件未接受指定的辅助功能操作。");
}

static void perform(id element, CFStringRef action) {
    if (AXUIElementPerformAction((__bridge AXUIElementRef)element, action) != kAXErrorSuccess)
        fail(@"此控件不支持指定动作。");
}

static id elementAt(id app, id window, CGRect frame, NSDictionary *request) {
    NSNumber *x = request[@"x"], *y = request[@"y"];
    if (![x isKindOfClass:NSNumber.class] || ![y isKindOfClass:NSNumber.class] ||
        !(x.doubleValue >= 0 && x.doubleValue < 1 && y.doubleValue >= 0 && y.doubleValue < 1))
        fail(@"坐标超出了共享窗口画面。");
    AXUIElementRef hit = NULL;
    AXError error = AXUIElementCopyElementAtPosition((__bridge AXUIElementRef)app,
        frame.origin.x + x.doubleValue * frame.size.width, frame.origin.y + y.doubleValue * frame.size.height, &hit);
    id element = CFBridgingRelease(hit);
    if (error != kAXErrorSuccess || !belongs(element, window)) fail(@"该位置的控件不属于共享窗口。");
    return element;
}

static void scrollElement(id element, id window, NSString *direction) {
    BOOL vertical = [@[@"up", @"down"] containsObject:direction];
    if (!vertical && ![@[@"left", @"right"] containsObject:direction]) fail(@"滚动方向无效。");
    BOOL forward = [direction isEqual:@"down"] || [direction isEqual:@"right"];
    for (int depth = 0; depth < 64 && belongs(element, window); depth++) {
        id bar = attribute(element, vertical ? kAXVerticalScrollBarAttribute : kAXHorizontalScrollBarAttribute);
        if (bar && belongs(bar, window)) {
            NSNumber *low = attribute(bar, kAXMinValueAttribute), *high = attribute(bar, kAXMaxValueAttribute);
            NSNumber *value = attribute(bar, kAXValueAttribute);
            if (!value || !low || !high || !(low.doubleValue < high.doubleValue)) fail(@"滚动条未提供有效范围。");
            double step = (high.doubleValue - low.doubleValue) * (forward ? 0.2 : -0.2);
            setAttribute(bar, kAXValueAttribute, @(fmin(high.doubleValue, fmax(low.doubleValue, value.doubleValue + step))));
            return;
        }
        if ([element isEqual:window]) break;
        element = attribute(element, kAXParentAttribute);
    }
    fail(@"该位置没有可操作的滚动条。");
}

static void keyAction(id element, NSString *key) {
    if ([key isEqual:@"Return"]) { perform(element, kAXConfirmAction); return; }
    if ([key isEqual:@"Space"]) { perform(element, kAXPressAction); return; }
    if (![@[@"Cmd+A", @"Home", @"End"] containsObject:key]) fail(@"此快捷键不支持限定到单个控件。");
    NSNumber *count = attribute(element, kAXNumberOfCharactersAttribute);
    if (![count isKindOfClass:NSNumber.class] || count.integerValue < 0) fail(@"当前控件未提供文本选择范围。");
    CFRange range = CFRangeMake([key isEqual:@"End"] ? count.integerValue : 0, [key isEqual:@"Cmd+A"] ? count.integerValue : 0);
    id selection = CFBridgingRelease(AXValueCreate(kAXValueCFRangeType, &range));
    setAttribute(element, kAXSelectedTextRangeAttribute, selection);
}

static void control(NSDictionary *request, SCWindow *target, NSArray<SCWindow *> *windows) {
    if (!AXIsProcessTrusted()) fail(@"请在系统设置的“隐私与安全性 → 辅助功能”中允许 MixHarness 的窗口操作。");
    if (![request[@"bounds"] isEqual:bounds(target.frame)]) fail(@"窗口位置或大小已变化，请重新读取画面。");
    id app = CFBridgingRelease(AXUIElementCreateApplication(target.owningApplication.processID));
    AXUIElementSetMessagingTimeout((__bridge AXUIElementRef)app, 1.5);
    id window = accessibilityWindow(app, target, windows);
    NSString *op = request[@"op"];
    BOOL pointed = [op isEqual:@"click"] || [op isEqual:@"scroll"];
    id element = pointed ? elementAt(app, window, target.frame, request) : attribute(app, kAXFocusedUIElementAttribute);
    if (!belongs(element, window) || (!pointed && ![attribute(app, kAXFocusedWindowAttribute) isEqual:window]))
        fail(@"当前输入焦点不属于共享窗口。");
    if ([attribute(element, kAXSubroleAttribute) isEqual:(__bridge NSString *)kAXSecureTextFieldSubrole])
        fail(@"该控件是受保护的输入框。");
    checkLiveWindow(target, request[@"target"]);
    if ([op isEqual:@"click"]) {
        NSString *role = attribute(element, kAXRoleAttribute);
        if ([role isEqual:(__bridge NSString *)kAXTextAreaRole] || [role isEqual:(__bridge NSString *)kAXTextFieldRole])
            setAttribute(element, kAXFocusedAttribute, @YES);
        else perform(element, kAXPressAction);
    } else if ([op isEqual:@"scroll"]) {
        scrollElement(element, window, request[@"direction"]);
    } else if ([op isEqual:@"type_text"]) {
        NSString *text = request[@"text"];
        if (![text isKindOfClass:NSString.class] || !text.length || text.length > 4096) fail(@"输入文本长度无效。");
        setAttribute(element, kAXSelectedTextAttribute, text);
    } else if ([op isEqual:@"press_key"]) {
        keyAction(element, request[@"key"]);
    } else fail(@"未知窗口操作。");
    reply(@{@"performed":op});
}

static void run(NSDictionary *request) API_AVAILABLE(macos(14.2));
static void run(NSDictionary *request) {
    NSString *op = request[@"op"];
    if ([op isEqual:@"permissions"]) {
        NSString *prompt = request[@"request"];
        if ([prompt isEqual:@"screen"]) CGRequestScreenCaptureAccess();
        if ([prompt isEqual:@"accessibility"]) AXIsProcessTrustedWithOptions((__bridge CFDictionaryRef)@{(__bridge NSString *)kAXTrustedCheckOptionPrompt:@YES});
        reply(@{@"screen":[NSNumber numberWithBool:CGPreflightScreenCaptureAccess()],
                @"accessibility":[NSNumber numberWithBool:AXIsProcessTrusted()]});
    }
    if (![@[@"list", @"describe", @"capture", @"click", @"scroll", @"type_text", @"press_key"] containsObject:op]) fail(@"未知窗口请求。");
    if (!CGPreflightScreenCaptureAccess()) fail(@"读取窗口需要 macOS 的屏幕录制权限。请打开系统设置中的“隐私与安全性 → 屏幕与系统音频录制”。");
    [SCShareableContent getShareableContentExcludingDesktopWindows:YES onScreenWindowsOnly:YES completionHandler:^(SCShareableContent *content, NSError *error) {
        if (error || !content) fail(error.localizedDescription ?: @"无法列出可共享窗口。");
        NSMutableArray *choices = [NSMutableArray array];
        for (SCWindow *window in content.windows) {
            NSString *key = windowKey(window);
            if (!key) continue;
            if ([op isEqual:@"list"]) { [choices addObject:descriptionOf(window, key)]; continue; }
            if (![key isEqual:request[@"target"]]) continue;
            checkLiveWindow(window, key);
            if ([op isEqual:@"describe"]) reply(descriptionOf(window, key));
            if ([op isEqual:@"capture"]) { capture(window, key); return; }
            control(request, window, content.windows);
        }
        if ([op isEqual:@"list"]) reply(@{@"windows":choices});
        fail(@"所选窗口已关闭或不再可共享，请重新选择。");
    }];
    dispatch_main();
}

int main(void) {
    @autoreleasepool {
        NSData *data = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
        NSError *error = nil;
        id request = [NSJSONSerialization JSONObjectWithData:data options:0 error:&error];
        if (![request isKindOfClass:NSDictionary.class] || ![request[@"op"] isKindOfClass:NSString.class])
            fail(error.localizedDescription ?: @"窗口请求格式错误。");
        if (@available(macOS 14.2, *)) run(request);
        else fail(@"共享窗口需要 macOS 14.2 或更新版本。");
    }
    return 0;
}
